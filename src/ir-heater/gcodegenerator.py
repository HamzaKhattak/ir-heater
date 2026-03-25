from __future__ import annotations

import argparse
import csv
from pathlib import Path


def read_motion_csv(csv_path: Path) -> list[tuple[float, float, float, float]]:
    if not csv_path.exists():
        raise ValueError(f"CSV file not found: {csv_path}")

    points: list[tuple[float, float, float, float]] = []
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("CSV is empty or missing a header row")

        required = {"x", "y", "z", "speed"}
        available = {name.strip().lower() for name in reader.fieldnames if name}
        missing = required - available
        if missing:
            missing_columns = ", ".join(sorted(missing))
            raise ValueError(f"CSV missing required columns: {missing_columns}")

        for line_number, row in enumerate(reader, start=2):
            normalized = {
                (key.strip().lower() if key else ""): (value or "")
                for key, value in row.items()
            }
            try:
                x = float(normalized.get("x", "").strip())
                y = float(normalized.get("y", "").strip())
                z = float(normalized.get("z", "").strip())
                speed = float(normalized.get("speed", "").strip())
            except ValueError as exc:
                raise ValueError(f"Invalid numeric value at CSV line {line_number}") from exc

            if speed <= 0:
                raise ValueError(f"Speed must be greater than 0 at CSV line {line_number}")

            points.append((x, y, z, speed))

    if not points:
        raise ValueError("CSV must include at least one motion row")

    return points


def build_gcode(points: list[tuple[float, float, float, float]], loops: int) -> str:
    lines = [
        "; Simple G-code loop from CSV",
        "G21 ; Set units to millimeters",
        "G90 ; Use absolute positioning",
    ]

    for _ in range(loops):
        for x, y, z, speed in points:
            lines.append(f"G1 F{speed:.2f}")
            lines.append(f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f}")

    lines.append("M2 ; Program end")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate G-code from a CSV of positions and speeds."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="CSV with x,y,z,speed columns",
    )
    parser.add_argument(
        "--loops",
        type=int,
        default=10,
        help="How many times to repeat the CSV motion sequence",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("loop.gcode"),
        help="Output G-code file path",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.loops < 1:
        raise ValueError("--loops must be at least 1")

    points = read_motion_csv(args.csv)
    gcode = build_gcode(points=points, loops=args.loops)

    args.output.write_text(gcode, encoding="utf-8")
    print(
        f"Wrote {args.output} with {args.loops} loops over "
        f"{len(points)} CSV motion rows."
    )


if __name__ == "__main__":
    main()
