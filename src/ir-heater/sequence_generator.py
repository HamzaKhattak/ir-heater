from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Position:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class PositionPairSpec:
    a: Position
    b: Position
    duration_s: float
    current_a: float
    voltage_v: float
    feedrate: float
    step_s: float


@dataclass(frozen=True)
class SequenceRow:
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


def _get_value(row: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in row:
            return row[key]
    return ""


def _parse_float(row: dict[str, str], keys: tuple[str, ...], line_number: int) -> float:
    value = _get_value(row, keys)
    if value == "":
        raise ValueError(f"Missing value for {keys} at line {line_number}")
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid numeric value for {keys} at line {line_number}") from exc


def read_pair_specs_csv(
    pairs_csv: Path,
    default_feedrate: float,
    default_step_s: float,
) -> list[PositionPairSpec]:
    if not pairs_csv.exists():
        raise ValueError(f"Pairs CSV file not found: {pairs_csv}")
    if default_feedrate <= 0:
        raise ValueError("default_feedrate must be > 0")
    if default_step_s <= 0:
        raise ValueError("default_step_s must be > 0")

    specs: list[PositionPairSpec] = []
    with pairs_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("Pairs CSV is empty or missing header")

        required = {"ax", "ay", "az", "bx", "by", "bz", "duration_s", "current_a", "voltage_v"}
        normalized_fields = {name.strip().lower() for name in reader.fieldnames if name}
        missing = required - normalized_fields
        if missing:
            missing_fields = ", ".join(sorted(missing))
            raise ValueError(f"Pairs CSV missing required columns: {missing_fields}")

        for line_number, row in enumerate(reader, start=2):
            normalized = _normalized_row(row)

            ax = _parse_float(normalized, ("ax",), line_number)
            ay = _parse_float(normalized, ("ay",), line_number)
            az = _parse_float(normalized, ("az",), line_number)
            bx = _parse_float(normalized, ("bx",), line_number)
            by = _parse_float(normalized, ("by",), line_number)
            bz = _parse_float(normalized, ("bz",), line_number)
            duration_s = _parse_float(normalized, ("duration_s",), line_number)
            current_a = _parse_float(normalized, ("current_a",), line_number)
            voltage_v = _parse_float(normalized, ("voltage_v",), line_number)

            feedrate_raw = _get_value(normalized, ("feedrate", "speed", "f"))
            feedrate = default_feedrate if feedrate_raw == "" else float(feedrate_raw)
            step_raw = _get_value(normalized, ("step_s", "time_step", "dt"))
            step_s = default_step_s if step_raw == "" else float(step_raw)

            if duration_s <= 0:
                raise ValueError(f"duration_s must be > 0 at line {line_number}")
            if current_a < 0:
                raise ValueError(f"current_a must be >= 0 at line {line_number}")
            if voltage_v < 0:
                raise ValueError(f"voltage_v must be >= 0 at line {line_number}")
            if feedrate <= 0:
                raise ValueError(f"feedrate must be > 0 at line {line_number}")
            if step_s <= 0:
                raise ValueError(f"step_s must be > 0 at line {line_number}")

            specs.append(
                PositionPairSpec(
                    a=Position(x=ax, y=ay, z=az),
                    b=Position(x=bx, y=by, z=bz),
                    duration_s=duration_s,
                    current_a=current_a,
                    voltage_v=voltage_v,
                    feedrate=feedrate,
                    step_s=step_s,
                )
            )

    if not specs:
        raise ValueError("Pairs CSV must contain at least one row")
    return specs


def rows_for_pair(spec: PositionPairSpec) -> list[SequenceRow]:
    rows: list[SequenceRow] = []
    elapsed = 0.0
    use_a = True

    # Alternate A/B moves until this pair's requested duration is consumed.
    while elapsed < spec.duration_s:
        dt = min(spec.step_s, spec.duration_s - elapsed)
        pos = spec.a if use_a else spec.b
        rows.append(
            SequenceRow(
                time_s=dt,
                current_a=spec.current_a,
                voltage_v=spec.voltage_v,
                x=pos.x,
                y=pos.y,
                z=pos.z,
                feedrate=spec.feedrate,
            )
        )
        elapsed += dt
        use_a = not use_a

    return rows


def generate_sequence_rows(specs: list[PositionPairSpec]) -> list[SequenceRow]:
    rows: list[SequenceRow] = []
    for spec in specs:
        rows.extend(rows_for_pair(spec))
    return rows


def write_sequence_csv(rows: list[SequenceRow], output_csv: Path) -> None:
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "current", "voltage", "x", "y", "z", "feedrate"])
        for row in rows:
            writer.writerow(
                [
                    f"{row.time_s:.6f}",
                    f"{row.current_a:.6f}",
                    f"{row.voltage_v:.6f}",
                    f"{row.x:.6f}",
                    f"{row.y:.6f}",
                    f"{row.z:.6f}",
                    f"{row.feedrate:.6f}",
                ]
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate sequence CSV for sequence_runner.read_sequence_csv from position pairs."
        )
    )
    parser.add_argument(
        "--pairs-csv",
        type=Path,
        required=True,
        help="Input CSV describing position pairs and per-pair settings",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("sequence.csv"),
        help="Generated output CSV compatible with sequence_runner",
    )
    parser.add_argument(
        "--default-feedrate",
        type=float,
        default=1200.0,
        help="Default feedrate when pair row does not define feedrate",
    )
    parser.add_argument(
        "--default-step-s",
        type=float,
        default=0.5,
        help="Default time in seconds between A/B moves when pair row does not define step_s",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    specs = read_pair_specs_csv(
        pairs_csv=args.pairs_csv,
        default_feedrate=args.default_feedrate,
        default_step_s=args.default_step_s,
    )
    rows = generate_sequence_rows(specs)
    write_sequence_csv(rows, args.output)
    print(f"Wrote {args.output} with {len(rows)} rows from {len(specs)} pair specs.")


if __name__ == "__main__":
    main()