"""Graphical front-end for the IR-heater sequence runner.

Launch via:
    python main.py gui
or directly (when run from the src/ir-heater directory):
    python gui.py

The sequence runs in a background daemon thread so the GUI never delays
hardware timing.  Plot updates are driven by a 100 ms polling timer on the
main thread — well below any timing-sensitive interval.
"""
from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# ---------------------------------------------------------------------------
# Import sequence_runner from the same directory regardless of cwd
# ---------------------------------------------------------------------------
_SR_DIR = Path(__file__).parent
if str(_SR_DIR) not in sys.path:
    sys.path.insert(0, str(_SR_DIR))

from sequence_generator import (  # noqa: E402 – path patched above
    generate_sequence_rows,
    read_pair_specs_csv,
    write_sequence_csv,
)
from sequence_runner import (  # noqa: E402 – path patched above
    PrinterController,
    SequenceStep,
    connect_dps,
    read_sequence_csv,
    run_sequence,
)

_DEFAULT_FEEDRATE = 1200.0
_POLL_MS = 100  # GUI update interval in milliseconds


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("IR Heater Sequence Runner")
        self.resizable(True, True)

        self._looped_steps: list[SequenceStep] = []
        self._generated_csv_path: Path | None = None
        self._stop_event = threading.Event()
        self._progress_q: queue.Queue[int | str] = queue.Queue()
        self._worker: threading.Thread | None = None

        self._build_controls()
        self._build_plots()
        self.after(_POLL_MS, self._poll_progress)

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _build_controls(self) -> None:
        ctrl = ttk.Frame(self, padding=8)
        ctrl.grid(row=0, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)

        # --- Pairs CSV row ---
        ttk.Label(ctrl, text="Pairs CSV:").grid(row=0, column=0, sticky="w")
        self._pairs_csv_var = tk.StringVar()
        ttk.Entry(ctrl, textvariable=self._pairs_csv_var, width=55).grid(
            row=0, column=1, padx=4, sticky="ew"
        )
        ttk.Button(ctrl, text="Browse…", command=self._browse_pairs_csv).grid(row=0, column=2)
        ctrl.columnconfigure(1, weight=1)

        # --- Connection settings ---
        conn = ttk.Frame(ctrl)
        conn.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        ttk.Label(conn, text="Modbus port:").pack(side="left")
        self._modbus_port_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self._modbus_port_var, width=8).pack(side="left", padx=2)

        ttk.Label(conn, text="Addr:").pack(side="left", padx=(6, 0))
        self._modbus_addr_var = tk.StringVar(value="1")
        ttk.Entry(conn, textvariable=self._modbus_addr_var, width=4).pack(side="left", padx=2)

        ttk.Label(conn, text="Baud:").pack(side="left", padx=(6, 0))
        self._modbus_baud_var = tk.StringVar(value="9600")
        ttk.Entry(conn, textvariable=self._modbus_baud_var, width=7).pack(side="left", padx=2)

        ttk.Label(conn, text="Printer port:").pack(side="left", padx=(10, 0))
        self._printer_port_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self._printer_port_var, width=8).pack(side="left", padx=2)

        ttk.Label(conn, text="Baud:").pack(side="left", padx=(6, 0))
        self._printer_baud_var = tk.StringVar(value="250000")
        ttk.Entry(conn, textvariable=self._printer_baud_var, width=7).pack(side="left", padx=2)

        # --- Generator options ---
        gen = ttk.Frame(ctrl)
        gen.grid(row=2, column=0, columnspan=3, sticky="ew", pady=4)

        ttk.Label(gen, text="Default feedrate:").pack(side="left")
        self._feedrate_var = tk.StringVar(value=str(_DEFAULT_FEEDRATE))
        ttk.Entry(gen, textvariable=self._feedrate_var, width=7).pack(side="left", padx=2)

        ttk.Label(gen, text="Default transition:").pack(side="left", padx=(8, 0))
        self._transition_var = tk.StringVar(value="5.0")
        ttk.Entry(gen, textvariable=self._transition_var, width=6).pack(side="left", padx=2)

        ttk.Label(gen, text="Loops:").pack(side="left", padx=(8, 0))
        self._loops_var = tk.StringVar(value="1")
        ttk.Entry(gen, textvariable=self._loops_var, width=5).pack(side="left", padx=2)

        # --- Sequence options ---
        opts = ttk.Frame(ctrl)
        opts.grid(row=3, column=0, columnspan=3, sticky="ew", pady=4)

        ttk.Label(opts, text="Time mode:").pack(side="left")
        self._time_mode_var = tk.StringVar(value="step")
        ttk.Combobox(
            opts,
            textvariable=self._time_mode_var,
            values=["step", "absolute"],
            width=8,
            state="readonly",
        ).pack(side="left", padx=2)

        self._dry_run_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Dry run", variable=self._dry_run_var).pack(
            side="left", padx=(10, 0)
        )

        self._return_to_origin_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts, text="Return to 0,0,0 after run", variable=self._return_to_origin_var
        ).pack(side="left", padx=(10, 0))

        # --- Run / Stop + status ---
        actions = ttk.Frame(ctrl)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 2))

        self._run_btn = ttk.Button(actions, text="Run", command=self._on_run, width=10)
        self._run_btn.pack(side="left")

        self._stop_btn = ttk.Button(
            actions, text="Stop", command=self._on_stop, state="disabled", width=10
        )
        self._stop_btn.pack(side="left", padx=6)

        self._status_var = tk.StringVar(value="Ready")
        ttk.Label(actions, textvariable=self._status_var).pack(side="left", padx=8)

        # --- Progress bar ---
        self._progress_var = tk.DoubleVar(value=0.0)
        ttk.Progressbar(ctrl, variable=self._progress_var, maximum=100.0).grid(
            row=5, column=0, columnspan=3, sticky="ew", pady=(0, 4)
        )

    def _build_plots(self) -> None:
        fig = Figure(figsize=(11, 3.5), tight_layout=True)
        self._ax_pos = fig.add_subplot(1, 3, 1)
        self._ax_volt = fig.add_subplot(1, 3, 2)
        self._ax_curr = fig.add_subplot(1, 3, 3)

        for ax, title, ylabel in (
            (self._ax_pos, "Position (mm)", "mm"),
            (self._ax_volt, "Voltage", "V"),
            (self._ax_curr, "Current", "A"),
        ):
            ax.set_title(title, fontsize=9)
            ax.set_xlabel("Step", fontsize=8)
            ax.set_ylabel(ylabel, fontsize=8)
            ax.tick_params(labelsize=7)
            ax.grid(True, linewidth=0.4)

        # Vertical progress markers (hidden until a run starts)
        self._vline_pos = self._ax_pos.axvline(x=0, color="red", linewidth=1, visible=False)
        self._vline_volt = self._ax_volt.axvline(x=0, color="red", linewidth=1, visible=False)
        self._vline_curr = self._ax_curr.axvline(x=0, color="red", linewidth=1, visible=False)

        self._fig = fig
        canvas = FigureCanvasTkAgg(fig, master=self)
        canvas.draw()
        canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self.rowconfigure(1, weight=1)
        self._canvas = canvas

    # ------------------------------------------------------------------
    # User interactions
    # ------------------------------------------------------------------

    def _browse_pairs_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="Select pairs CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        self._pairs_csv_var.set(path)
        self._load_pairs_csv(Path(path))

    def _load_pairs_csv(self, path: Path) -> None:
        try:
            feedrate = float(self._feedrate_var.get() or _DEFAULT_FEEDRATE)
            transition = float(self._transition_var.get() or 5.0)
            loops = max(1, int(self._loops_var.get() or 1))
        except ValueError as exc:
            messagebox.showerror("Input Error", f"Invalid parameter: {exc}")
            return

        try:
            specs = read_pair_specs_csv(
                pairs_csv=path,
                default_feedrate=feedrate,
                default_transition_s=transition,
            )
        except Exception as exc:
            messagebox.showerror("Pairs CSV Error", str(exc))
            return

        # Generate the sequence
        try:
            rows = generate_sequence_rows(specs)
        except Exception as exc:
            messagebox.showerror("Generation Error", str(exc))
            return

        # Multiply rows by loop count
        looped_rows = rows * loops

        # Write to a generated file named <original>_loops_<count>.csv
        output_path = path.with_stem(f"{path.stem}_loops_{loops}")
        try:
            write_sequence_csv(looped_rows, output_path)
        except Exception as exc:
            messagebox.showerror("Write Error", str(exc))
            return

        # Read back the generated sequence
        try:
            self._looped_steps = read_sequence_csv(output_path, default_feedrate=feedrate)
        except Exception as exc:
            messagebox.showerror("Sequence Error", str(exc))
            return

        self._generated_csv_path = output_path
        self._plot_planned(self._looped_steps)
        self._status_var.set(
            f"Generated {output_path.name}: {len(self._looped_steps)} steps"
        )

    def _plot_planned(self, steps: list[SequenceStep]) -> None:
        xs = list(range(len(steps)))
        x_vals = [s.x for s in steps]
        y_vals = [s.y for s in steps]
        z_vals = [s.z for s in steps]
        volts = [s.voltage_v for s in steps]
        amps = [s.current_a for s in steps]

        for ax in (self._ax_pos, self._ax_volt, self._ax_curr):
            ax.cla()
            ax.set_xlabel("Step", fontsize=8)
            ax.tick_params(labelsize=7)
            ax.grid(True, linewidth=0.4)

        self._ax_pos.set_title("Position (mm)", fontsize=9)
        self._ax_pos.set_ylabel("mm", fontsize=8)
        self._ax_pos.plot(xs, x_vals, label="X", linewidth=1)
        self._ax_pos.plot(xs, y_vals, label="Y", linewidth=1)
        self._ax_pos.plot(xs, z_vals, label="Z", linewidth=1)
        self._ax_pos.legend(fontsize=7)

        self._ax_volt.set_title("Voltage", fontsize=9)
        self._ax_volt.set_ylabel("V", fontsize=8)
        self._ax_volt.plot(xs, volts, color="tab:orange", linewidth=1)

        self._ax_curr.set_title("Current", fontsize=9)
        self._ax_curr.set_ylabel("A", fontsize=8)
        self._ax_curr.plot(xs, amps, color="tab:green", linewidth=1)

        # Re-create progress vlines after cla()
        self._vline_pos = self._ax_pos.axvline(x=0, color="red", linewidth=1, visible=False)
        self._vline_volt = self._ax_volt.axvline(x=0, color="red", linewidth=1, visible=False)
        self._vline_curr = self._ax_curr.axvline(x=0, color="red", linewidth=1, visible=False)

        self._canvas.draw_idle()

    def _on_run(self) -> None:
        if not self._looped_steps:
            messagebox.showwarning(
                "No sequence", "Please load a pairs CSV first to generate a sequence."
            )
            return

        dry_run = self._dry_run_var.get()
        if not dry_run and not self._modbus_port_var.get().strip():
            messagebox.showerror(
                "Missing port", "Modbus port is required when not using dry run."
            )
            return

        self._stop_event.clear()
        self._run_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._status_var.set("Running…")
        self._progress_var.set(0.0)

        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()

    def _on_stop(self) -> None:
        self._stop_event.set()
        self._status_var.set("Stopping…")
        self._stop_btn.configure(state="disabled")

    # ------------------------------------------------------------------
    # Worker thread — hardware interaction happens here, not on GUI thread
    # ------------------------------------------------------------------

    def _run_worker(self) -> None:
        try:
            dry_run = self._dry_run_var.get()

            if dry_run:
                dps = None
                printer = None
            else:
                ini_path = Path(__file__).with_name("dps5005_limits.ini")
                try:
                    dps = connect_dps(
                        modbus_port=self._modbus_port_var.get().strip(),
                        ini_path=ini_path,
                        address=int(self._modbus_addr_var.get() or 1),
                        baudrate=int(self._modbus_baud_var.get() or 9600),
                    )
                except Exception as exc:
                    raise RuntimeError(f"Failed to connect DPS: {exc}") from exc

                printer_port = self._printer_port_var.get().strip()
                if printer_port:
                    try:
                        printer = PrinterController(
                            printer_port, int(self._printer_baud_var.get() or 250000)
                        )
                    except Exception as exc:
                        raise RuntimeError(f"Failed to connect printer: {exc}") from exc
                else:
                    printer = None

            def _on_step(index: int, total: int) -> None:
                # Called from the worker thread — only a fast queue put, no GUI calls
                self._progress_q.put(index)

            run_sequence(
                self._looped_steps,
                dps=dps,
                printer=printer,
                time_mode=self._time_mode_var.get(),
                dry_run=dry_run,
                stop_event=self._stop_event,
                on_step=_on_step,
                return_to_origin=self._return_to_origin_var.get(),
            )
            self._progress_q.put("done")

        except Exception as exc:
            self._progress_q.put(f"error:{exc}")

    # ------------------------------------------------------------------
    # Progress polling — runs on the main (GUI) thread via after()
    # ------------------------------------------------------------------

    def _poll_progress(self) -> None:
        total = max(len(self._looped_steps), 1)
        redraw = False
        try:
            while True:
                msg = self._progress_q.get_nowait()
                if isinstance(msg, int):
                    pct = msg / total * 100.0
                    self._progress_var.set(pct)
                    self._status_var.set(f"Step {msg} / {total}")
                    self._set_vlines(msg - 1)
                    redraw = True
                elif msg == "done":
                    self._progress_var.set(100.0)
                    self._status_var.set("Done")
                    self._run_btn.configure(state="normal")
                    self._stop_btn.configure(state="disabled")
                    redraw = True
                elif isinstance(msg, str) and msg.startswith("error:"):
                    err = msg[6:]
                    self._status_var.set(f"Error: {err}")
                    messagebox.showerror("Sequence error", err)
                    self._run_btn.configure(state="normal")
                    self._stop_btn.configure(state="disabled")
        except queue.Empty:
            pass

        if redraw:
            self._canvas.draw_idle()

        self.after(_POLL_MS, self._poll_progress)

    def _set_vlines(self, step_index: int) -> None:
        for vline in (self._vline_pos, self._vline_volt, self._vline_curr):
            vline.set_xdata([step_index, step_index])
            vline.set_visible(True)


# ---------------------------------------------------------------------------

def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
