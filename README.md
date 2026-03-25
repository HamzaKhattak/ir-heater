# IR Heater Sequence Runner

This project can run synchronized control steps for:

- A 3D printer (through `printrun` / `printcore`)
- A DPS power supply (through Modbus using `dps_modbus.py`)

The runner reads one CSV that includes timing, electrical setpoints, and motion setpoints.

## CSV Format

Required columns:

- `time` (or `time_s`, `dt`, `duration`)
- `current` (or `current_a`, `i`, `amps`)
- `voltage` (or `voltage_v`, `v`, `volts`)
- `x`
- `y`
- `z`

Optional column:

- `feedrate` (or `speed`, `f`)

Example:

```csv
time,current,voltage,x,y,z,feedrate
1.5,1.2,12.0,10,10,1,1200
2.0,1.5,14.0,20,10,1,1400
1.0,1.0,10.0,20,20,1,1200
```

## Main Entrypoint

`main.py` now acts as the unified CLI entrypoint.

Supported modes:

- `run` to execute a sequence CSV on the printer and DPS
- `generate` to build a sequence CSV from position-pair definitions
- no subcommand, which defaults to `run` for backward compatibility

## Run

Explicit run mode:

```bash
uv run main.py \
	run \
	--csv schedule.csv \
	--modbus-port COM4 \
	--printer-port COM6 \
	--loops 3
```

Backward-compatible form:

```bash
uv run main.py \
	--csv schedule.csv \
	--modbus-port COM4 \
	--printer-port COM6 \
	--loops 3
```

Important options:

- `--time-mode step|absolute`
- `--dry-run` to validate the CSV and print planned steps without sending hardware commands
- `--default-feedrate 1200`

## Loop Helper

`--loops N` repeats your CSV sequence N times, similar to loop generation behavior in `gcodegenerator.py`.
## Sequence Generator

Use `main.py generate` to create schedule CSV files that are directly compatible with `read_sequence_csv` in `sequence_runner.py`.

Run:

```bash
uv run main.py generate \
	--pairs-csv pair_specs.csv \
	--output sequence.csv
```

Input pairs CSV required columns:

- `ax, ay, az`
- `bx, by, bz`
- `duration_s`
- `current_a`
- `voltage_v`

Optional pair columns:

- `feedrate` (or `speed`, `f`)
- `step_s` (or `time_step`, `dt`)

Behavior:

- For each pair row, generator alternates moves between `A(ax,ay,az)` and `B(bx,by,bz)`.
- It keeps alternating for that pair's `duration_s`, then proceeds to the next pair row.
- Each pair can have different `duration_s`, `current_a`, and `voltage_v`.
- Output CSV columns are: `time,current,voltage,x,y,z,feedrate`.

Example input (`pair_specs.csv`):

```csv
ax,ay,az,bx,by,bz,duration_s,current_a,voltage_v,feedrate,step_s
10,10,1,20,10,1,15,1.20,12.0,1200,0.5
20,20,1,30,20,1,8,0.90,10.5,1000,0.25
```