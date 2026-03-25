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
	--default-transition-s 1.5 \
	--output sequence.csv
```

Input pairs CSV required columns:

- `ax, ay, az`
- `bx, by, bz`
- `duration_s`
- `current_a`
- `voltage_v`

Optional pair column:

- `feedrate` (or `speed`, `f`) — defaults to `--default-feedrate` if omitted
- `transition_s` (or `transition_to_next_s`, `move_to_next_s`) — time in seconds to move from this pair section to the next pair's `A` point

Behavior:

- For each pair row, the generator computes the exact travel time between A and B from geometry and feedrate:

  $t_{travel} = \dfrac{\sqrt{\Delta x^2 + \Delta y^2 + \Delta z^2} \times 60}{feedrate}$

  where feedrate is in mm/min (standard G-code).
- Each CSV `time` value is set to exactly $t_{travel}$, so the runner sleeps for precisely as long as the printer needs to complete each move — no idle waiting, no queued-up commands mid-move.
- The generator alternates A → B → A → B until the pair's `duration_s` is consumed, then moves to the next pair row.
- Between pair sections, the generator inserts a transition move from the previous section's last reached point to the next section's `A` point.
- That transition uses the specified `transition_s` (or `--default-transition-s`), and the transition feedrate is computed so the move takes exactly that time.
- Each pair can have different `duration_s`, `current_a`, `voltage_v`, and `feedrate`.
- A and B must not be the same position (zero distance produces an error).
- Output CSV columns are: `time,current,voltage,x,y,z,feedrate`.

Example input (`pair_specs.csv`):

```csv
ax,ay,az,bx,by,bz,duration_s,current_a,voltage_v,feedrate,transition_s
10,10,1,20,10,1,15,1.20,12.0,1200,2.0
20,20,1,30,20,1,8,0.90,10.5,1000,1.0
```