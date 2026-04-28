from __future__ import annotations

import argparse
import csv
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dps_modbus import Dps5005, Import_limits, Serial_modbus

try:
    from printrun.printcore import printcore
except Exception:  # pragma: no cover - depends on local system install
    printcore = None


@dataclass(frozen=True)
class SequenceStep:
    time_s: float
    current_a: float
    voltage_v: float
    x: float
    y: float
    z: float
    feedrate: float


def _normalized_row(row: dict[str, str | None]) -> dict[str, str]:
    return {
        (key.strip().lower() if key else ""): (value or "").strip()
        for key, value in row.items()
    }


def _first_value(row: dict[str, str], aliases: tuple[str, ...]) -> str:
    for name in aliases:
        if name in row:
            return row[name]
    return ""


def _parse_float(row: dict[str, str], aliases: tuple[str, ...], line_number: int) -> float:
    value = _first_value(row, aliases)
    if value == "":
        names = ", ".join(aliases)
        raise ValueError(f"Missing value for one of [{names}] at CSV line {line_number}")
    try:
        return float(value)
    except ValueError as exc:
        names = ", ".join(aliases)
        raise ValueError(f"Invalid numeric value for [{names}] at CSV line {line_number}") from exc


def read_sequence_csv(csv_path: Path, default_feedrate: float) -> list[SequenceStep]:
    if not csv_path.exists():
        raise ValueError(f"CSV file not found: {csv_path}")

    steps: list[SequenceStep] = []
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("CSV is empty or missing a header row")

        normalized_fields = {name.strip().lower() for name in reader.fieldnames if name}
        required_aliases = {
            "time": ("time", "time_s", "dt", "duration"),
            "current": ("current", "current_a", "i", "amps"),
            "voltage": ("voltage", "voltage_v", "v", "volts"),
            "x": ("x",),
            "y": ("y",),
            "z": ("z",),
        }
        missing = []
        for logical_name, aliases in required_aliases.items():
            if not any(alias in normalized_fields for alias in aliases):
                missing.append(logical_name)
        if missing:
            raise ValueError(f"CSV missing required columns: {', '.join(sorted(missing))}")

        for line_number, row in enumerate(reader, start=2):
            normalized = _normalized_row(row)
            time_s = _parse_float(normalized, required_aliases["time"], line_number)
            current_a = _parse_float(normalized, required_aliases["current"], line_number)
            voltage_v = _parse_float(normalized, required_aliases["voltage"], line_number)
            x = _parse_float(normalized, required_aliases["x"], line_number)
            y = _parse_float(normalized, required_aliases["y"], line_number)
            z = _parse_float(normalized, required_aliases["z"], line_number)

            feedrate_raw = _first_value(normalized, ("feedrate", "speed", "f"))
            feedrate = default_feedrate if feedrate_raw == "" else float(feedrate_raw)
            if feedrate <= 0:
                raise ValueError(f"Feedrate must be > 0 at CSV line {line_number}")

            if current_a < 0:
                raise ValueError(f"Current must be >= 0 at CSV line {line_number}")
            if voltage_v < 0:
                raise ValueError(f"Voltage must be >= 0 at CSV line {line_number}")

            steps.append(
                SequenceStep(
                    time_s=time_s,
                    current_a=current_a,
                    voltage_v=voltage_v,
                    x=x,
                    y=y,
                    z=z,
                    feedrate=feedrate,
                )
            )

    if not steps:
        raise ValueError("CSV must include at least one data row")

    return steps


def _interruptible_sleep(seconds: float, stop_event: threading.Event, poll_interval: float = 0.05) -> bool:
    """Sleep up to *seconds*, waking early if *stop_event* is set.

    Returns True if the sleep was interrupted by the event, False otherwise.
    """
    deadline = time.perf_counter() + seconds
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return False
        if stop_event.wait(timeout=min(poll_interval, remaining)):
            return True


def expand_loop_steps(steps: list[SequenceStep], loops: int) -> list[SequenceStep]:
    if loops < 1:
        raise ValueError("loops must be at least 1")
    if loops == 1:
        return list(steps)
    return steps * loops


class PrinterController:
    def __init__(self, serial_port: str, baudrate: int, connect_timeout_s: float = 10.0):
        if printcore is None:
            raise RuntimeError("printrun is not available. Install printrun to control the printer.")
        self._printer = printcore(serial_port, baudrate)
        deadline = time.time() + connect_timeout_s
        while not self._printer.online and time.time() < deadline:
            time.sleep(0.1)
        if not self._printer.online:
            self._printer.disconnect()
            raise RuntimeError("Failed to connect to printer before timeout")

    def send_move(self, x: float, y: float, z: float, feedrate: float) -> None:
        self._printer.send_now(f"G1 F{feedrate:.2f}")
        self._printer.send_now(f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f}")

    def disconnect(self) -> None:
        self._printer.disconnect()


def connect_dps(modbus_port: str, ini_path: Path, address: int, baudrate: int) -> Dps5005:
    serial_modbus = Serial_modbus(modbus_port, address, baudrate, 8)
    limits = Import_limits(str(ini_path))
    return Dps5005(serial_modbus, limits)


def run_sequence(
    steps: list[SequenceStep],
    dps: Dps5005 | None,
    printer: PrinterController | None,
    time_mode: str,
    dry_run: bool,
    stop_event: threading.Event | None = None,
    on_step: Callable[[int, int], None] | None = None,
    return_to_origin: bool = True,
) -> None:
    if time_mode not in {"step", "absolute"}:
        raise ValueError("time_mode must be one of: step, absolute")

    if not dry_run and dps is None:
        raise ValueError("dps instance is required when dry_run is False")

    if not dry_run:
        assert dps is not None  # narrowed by the guard above
        dps.onoff("w", 1)

    home_step: SequenceStep | None = steps[0] if steps else None
    previous_t = 0.0
    scheduled_elapsed = 0.0
    start_monotonic = time.perf_counter()
    last_voltage: float | None = None
    last_current: float | None = None
    try:
        for index, step in enumerate(steps, start=1):
            if stop_event is not None and stop_event.is_set():
                print("\nStop requested — aborting sequence.", flush=True)
                break
            if not dry_run:
                assert dps is not None  # narrowed by the entry guard
                if last_voltage != step.voltage_v:
                    dps.voltage_set("w", step.voltage_v)
                    last_voltage = step.voltage_v
                if last_current != step.current_a:
                    dps.current_set("w", step.current_a)
                    last_current = step.current_a

            if printer is not None:
                printer.send_move(step.x, step.y, step.z, step.feedrate)

            print(
                f"Step {index:04d}: "
                f"t={step.time_s:.3f}s "
                f"V={step.voltage_v:.3f} "
                f"I={step.current_a:.3f} "
                f"X={step.x:.3f} Y={step.y:.3f} Z={step.z:.3f} F={step.feedrate:.1f}"
            )

            if on_step is not None:
                on_step(index, len(steps))

            if time_mode == "step":
                delta_s = max(0.0, step.time_s)
            else:
                # Allow loop wrap-around: if time goes backwards, treat it as a
                # new loop iteration starting from 0.
                if step.time_s < previous_t:
                    previous_t = 0.0
                delta_s = step.time_s - previous_t
                previous_t = step.time_s

            scheduled_elapsed += delta_s
            target_time = start_monotonic + scheduled_elapsed
            wait_s = target_time - time.perf_counter()
            if wait_s > 0:
                if stop_event is not None:
                    interrupted = _interruptible_sleep(wait_s, stop_event)
                    if interrupted:
                        print("\nStop requested — aborting sequence.", flush=True)
                        break
                else:
                    time.sleep(wait_s)
    finally:
        if not dry_run and dps is not None:
            dps.onoff("w", 0)
            print("Light turned off.", flush=True)
        if printer is not None:
            if return_to_origin:
                print(
                    "Moving to origin: X=0.000 Y=0.000 Z=0.000",
                    flush=True,
                )
                origin_feedrate = home_step.feedrate if home_step is not None else 1200.0
                printer.send_move(0.0, 0.0, 0.0, origin_feedrate)
            elif home_step is not None:
                print(
                    f"Moving to initial position: "
                    f"X={home_step.x:.3f} Y={home_step.y:.3f} Z={home_step.z:.3f}",
                    flush=True,
                )
                printer.send_move(home_step.x, home_step.y, home_step.z, home_step.feedrate)
            printer.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run synchronized printer + DPS control from a CSV schedule."
    )
    parser.add_argument("--csv", type=Path, required=True, help="Input schedule CSV path")
    parser.add_argument(
        "--loops",
        type=int,
        default=1,
        help="Repeat the schedule this many times (helper loop generation)",
    )
    parser.add_argument(
        "--time-mode",
        choices=["step", "absolute"],
        default="step",
        help="Interpret time column as per-step delay or absolute schedule time",
    )
    parser.add_argument(
        "--default-feedrate",
        type=float,
        default=1200.0,
        help="Fallback feedrate when CSV row has no feedrate/speed",
    )

    parser.add_argument("--modbus-port", default="", help="DPS Modbus serial port (required unless --dry-run)")
    parser.add_argument("--modbus-address", type=int, default=1, help="DPS Modbus address")
    parser.add_argument("--modbus-baud", type=int, default=9600, help="DPS Modbus baud rate")
    parser.add_argument(
        "--limits-ini",
        type=Path,
        default=Path(__file__).with_name("dps5005_limits.ini"),
        help="Path to DPS limits ini",
    )

    parser.add_argument("--printer-port", default="", help="3D printer serial port")
    parser.add_argument("--printer-baud", type=int, default=250000, help="3D printer baud rate")

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print schedule without sending commands to hardware",
    )
    parser.add_argument(
        "--return-to-first-position",
        action="store_true",
        default=False,
        help="After the sequence ends return to the first CSV position instead of 0,0,0 (default: return to origin)",
    )
    return parser.parse_args()


def _stdin_stop_listener(stop_event: threading.Event) -> None:
    """Block on stdin until the user types 'q' and presses Enter, then signal a stop."""
    try:
        while True:
            line = sys.stdin.readline()
            if line.strip().lower() == "q":
                break
    except Exception:
        pass
    stop_event.set()


def main() -> None:
    args = parse_args()

    steps = read_sequence_csv(args.csv, default_feedrate=args.default_feedrate)
    looped_steps = expand_loop_steps(steps, args.loops)

    if args.dry_run:
        run_sequence(looped_steps, dps=None, printer=None, time_mode=args.time_mode, dry_run=True)
        return

    if not args.modbus_port.strip():
        raise SystemExit("error: --modbus-port is required when not using --dry-run")

    dps = connect_dps(
        modbus_port=args.modbus_port,
        ini_path=args.limits_ini,
        address=args.modbus_address,
        baudrate=args.modbus_baud,
    )
    printer = (
        PrinterController(args.printer_port, args.printer_baud)
        if args.printer_port.strip()
        else None
    )

    stop_event = threading.Event()
    listener = threading.Thread(target=_stdin_stop_listener, args=(stop_event,), daemon=True)
    listener.start()
    print("Sequence running. Type 'q' and press Enter at any time to stop.", flush=True)

    run_sequence(
        looped_steps,
        dps=dps,
        printer=printer,
        time_mode=args.time_mode,
        dry_run=False,
        stop_event=stop_event,
        return_to_origin=not args.return_to_first_position,
    )


if __name__ == "__main__":
    main()