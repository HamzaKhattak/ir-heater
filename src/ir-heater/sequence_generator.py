from __future__ import annotations

import argparse
import csv
import math
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
    transition_to_next_s: float


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
    default_transition_s: float,
) -> list[PositionPairSpec]:
    if not pairs_csv.exists():
        raise ValueError(f"Pairs CSV file not found: {pairs_csv}")
    if default_feedrate <= 0:
        raise ValueError("default_feedrate must be > 0")
    if default_transition_s < 0:
        raise ValueError("default_transition_s must be >= 0")

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
            transition_raw = _get_value(
                normalized,
                ("transition_s", "transition_to_next_s", "move_to_next_s"),
            )
            transition_to_next_s = (
                default_transition_s if transition_raw == "" else float(transition_raw)
            )

            if duration_s <= 0:
                raise ValueError(f"duration_s must be > 0 at line {line_number}")
            if current_a < 0:
                raise ValueError(f"current_a must be >= 0 at line {line_number}")
            if voltage_v < 0:
                raise ValueError(f"voltage_v must be >= 0 at line {line_number}")
            if feedrate <= 0:
                raise ValueError(f"feedrate must be > 0 at line {line_number}")
            if transition_to_next_s < 0:
                raise ValueError(f"transition_s must be >= 0 at line {line_number}")

            specs.append(
                PositionPairSpec(
                    a=Position(x=ax, y=ay, z=az),
                    b=Position(x=bx, y=by, z=bz),
                    duration_s=duration_s,
                    current_a=current_a,
                    voltage_v=voltage_v,
                    feedrate=feedrate,
                    transition_to_next_s=transition_to_next_s,
                )
            )

    if not specs:
        raise ValueError("Pairs CSV must contain at least one row")
    return specs


def _travel_time_s(spec: PositionPairSpec) -> float:
    """Time in seconds for one A→B (or B→A) move at the pair's feedrate.

    Feedrate is in mm/min (standard G-code convention), so divide by 60
    to convert to mm/s before dividing into distance.
    """
    dx = spec.b.x - spec.a.x
    dy = spec.b.y - spec.a.y
    dz = spec.b.z - spec.a.z
    distance_mm = math.sqrt(dx * dx + dy * dy + dz * dz)
    if distance_mm == 0.0:
        raise ValueError(
            f"Position A and B are identical for pair starting at A={spec.a}. "
            "Cannot compute travel time for a zero-distance move."
        )
    return distance_mm * 60.0 / spec.feedrate


def _distance_mm(start: Position, end: Position) -> float:
    dx = end.x - start.x
    dy = end.y - start.y
    dz = end.z - start.z
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _transition_feedrate(start: Position, end: Position, transition_s: float, fallback_feedrate: float) -> float:
    distance_mm = _distance_mm(start, end)
    if distance_mm == 0:
        return fallback_feedrate
    return distance_mm * 60.0 / transition_s


def rows_for_pair(spec: PositionPairSpec) -> list[SequenceRow]:
    travel_time_s = _travel_time_s(spec)
    rows: list[SequenceRow] = []
    epsilon = 1e-9
    if spec.duration_s + epsilon < travel_time_s:
        raise ValueError(
            "duration_s is shorter than one full A/B move at the configured feedrate; "
            "increase duration_s or reduce feedrate"
        )

    # Use integer move count + remainder to avoid float accumulation drift
    # causing accidental extra moves beyond duration_s.
    full_moves = int(math.floor((spec.duration_s + epsilon) / travel_time_s))
    for move_idx in range(full_moves):
        pos = spec.a if move_idx % 2 == 0 else spec.b
        rows.append(
            SequenceRow(
                time_s=travel_time_s,
                current_a=spec.current_a,
                voltage_v=spec.voltage_v,
                x=pos.x,
                y=pos.y,
                z=pos.z,
                feedrate=spec.feedrate,
            )
        )

    elapsed = full_moves * travel_time_s
    remainder_s = spec.duration_s - elapsed
    if remainder_s > epsilon and rows:
        # Fill remaining section time without another partial move.
        last = rows[-1]
        rows.append(
            SequenceRow(
                time_s=remainder_s,
                current_a=spec.current_a,
                voltage_v=spec.voltage_v,
                x=last.x,
                y=last.y,
                z=last.z,
                feedrate=spec.feedrate,
            )
        )

    return rows


def generate_sequence_rows(specs: list[PositionPairSpec]) -> list[SequenceRow]:
    rows: list[SequenceRow] = []
    for idx, spec in enumerate(specs):
        pair_rows = rows_for_pair(spec)
        rows.extend(pair_rows)

        if idx == len(specs) - 1:
            continue

        next_spec = specs[idx + 1]
        if spec.transition_to_next_s <= 0:
            continue

        # Move from the last point reached in this pair section to next pair's A
        # in exactly transition_to_next_s seconds.
        last = pair_rows[-1]
        start_pos = Position(last.x, last.y, last.z)
        end_pos = next_spec.a
        transition_feedrate = _transition_feedrate(
            start=start_pos,
            end=end_pos,
            transition_s=spec.transition_to_next_s,
            fallback_feedrate=next_spec.feedrate,
        )

        rows.append(
            SequenceRow(
                time_s=spec.transition_to_next_s,
                current_a=next_spec.current_a,
                voltage_v=next_spec.voltage_v,
                x=end_pos.x,
                y=end_pos.y,
                z=end_pos.z,
                feedrate=transition_feedrate,
            )
        )
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
        help="Default feedrate (mm/min) when pair row does not define feedrate",
    )
    parser.add_argument(
        "--default-transition-s",
        type=float,
        default=5.0,
        help="Default transition time in seconds between pair sections",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    specs = read_pair_specs_csv(
        pairs_csv=args.pairs_csv,
        default_feedrate=args.default_feedrate,
        default_transition_s=args.default_transition_s,
    )
    rows = generate_sequence_rows(specs)
    write_sequence_csv(rows, args.output)
    print(f"Wrote {args.output} with {len(rows)} rows from {len(specs)} pair specs.")


if __name__ == "__main__":
    main()