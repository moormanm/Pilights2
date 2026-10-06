"""Desktop window that shows the state of each channel and its GPIO pin."""

from __future__ import annotations

import threading

from .outputs import Output

COLORS = ("#e53935", "#43a047", "#fdd835", "#1e88e5", "#fb8c00", "#8e24aa", "#00acc1", "#f5f5f5")
OFF_COLOR = "#2b2b2b"
REFRESH_MS = 15


class GuiOutput(Output):
    """Tk must run on the main thread, so run() starts the work on a worker thread."""

    def __init__(self, pins, active_low: bool = False, title: str = "pilights"):
        import tkinter as tk  # Imported here so the Pi can run without Tk.

        self.channels = len(pins)
        self._pins = pins
        self._active_low = active_low
        self._mask = 0
        self._status = ""
        self.stop = threading.Event()

        self._root = tk.Tk()
        self._root.title(title)
        self._root.configure(bg="black")
        self._root.protocol("WM_DELETE_WINDOW", self.stop.set)
        self._canvas = tk.Canvas(self._root, width=90 * self.channels + 10, height=150,
                                 bg="black", highlightthickness=0)
        self._canvas.pack()
        self._lamps, self._levels = [], []
        for i, pin in enumerate(pins):
            x = 10 + 90 * i
            self._lamps.append(self._canvas.create_oval(x + 5, 10, x + 75, 80, fill=OFF_COLOR, outline="#555"))
            self._canvas.create_text(x + 40, 98, text=f"CH{i}", fill="white", font=("Helvetica", 12, "bold"))
            self._canvas.create_text(x + 40, 116, text=f"GPIO{pin}", fill="#aaa", font=("Helvetica", 11))
            self._levels.append(self._canvas.create_text(x + 40, 134, fill="#aaa", font=("Courier", 11)))
        self._status_var = tk.StringVar()
        tk.Label(self._root, textvariable=self._status_var, bg="black", fg="white",
                 font=("Helvetica", 12)).pack(fill="x", pady=(0, 8))
        self._drawn = None
        self._draw()

    def set_mask(self, mask: int) -> None:
        self._mask = mask

    def set_status(self, text: str) -> None:
        self._status = text

    def _draw(self) -> None:
        mask, status = self._mask, self._status
        if mask != self._drawn:
            for i in range(self.channels):
                on = bool(mask >> i & 1)
                high = on != self._active_low
                self._canvas.itemconfigure(self._lamps[i], fill=COLORS[i % len(COLORS)] if on else OFF_COLOR)
                self._canvas.itemconfigure(self._levels[i], text="HIGH" if high else "LOW")
            self._drawn = mask
        if self._status_var.get() != status:
            self._status_var.set(status)

    def _tick(self, worker: threading.Thread) -> None:
        self._draw()
        if self.stop.is_set() and not worker.is_alive():
            self._root.destroy()
            return
        self._root.after(REFRESH_MS, self._tick, worker)

    def run(self, work) -> None:
        """Call work() on a worker thread and show the window until it ends or the user closes it."""
        error = []

        def body():
            try:
                work()
            except BaseException as e:
                error.append(e)
            finally:
                self.stop.set()

        worker = threading.Thread(target=body, daemon=True)
        worker.start()
        self._root.after(REFRESH_MS, self._tick, worker)
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            self.stop.set()
        worker.join(timeout=2)
        if error:
            raise error[0]

    def close(self) -> None:
        self._mask = 0
