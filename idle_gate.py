"""
Decides when the GPU is free for a transcript: nothing full screen, no listed
game process, and the GPU nearly idle for ten minutes. Every input is an
injected callable, so tests need no GPU, no Windows and no clock.
"""
import csv
import io
import subprocess
import sys
import time

DEFAULT_GAMES = ["VALORANT-Win64-Shipping.exe"]  # agent_config.json: transcriber_block_processes
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # pythonw: no console flashing up per call
QUNS_BLOCKING = {2, 3, 4}  # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE


def _run(args) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=15,
                          creationflags=NO_WINDOW).stdout


def gpu_utilization():
    try:
        return int(_run(["nvidia-smi", "--query-gpu=utilization.gpu",
                         "--format=csv,noheader,nounits"]).split()[0])
    except Exception:
        return None


def fullscreen_busy() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    state = ctypes.c_int(0)
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return False
    return state.value in QUNS_BLOCKING


def game_running(names, output=None) -> bool:
    if output is None:
        try:
            output = _run(["tasklist", "/fo", "csv", "/nh"])
        except Exception:
            return False  # cannot tell: full screen and GPU load still guard
    wanted = {n.lower() for n in names}
    return any(row and row[0].lower() in wanted for row in csv.reader(io.StringIO(output)))


class IdleGate:
    def __init__(self, sample=gpu_utilization, busy=fullscreen_busy, game=lambda: False,
                 clock=time.monotonic, quiet_seconds=600, threshold=15):
        self._sample, self._busy, self._game, self._clock = sample, busy, game, clock
        self._quiet, self._threshold = quiet_seconds, threshold
        self._since = None  # start of the current run of clean observations

    def observe(self) -> None:
        now = self._clock()
        s = self._sample()
        if s is None or s >= self._threshold or self._busy() or self._game():
            self._since = None
        elif self._since is None:
            self._since = now

    def ready(self) -> bool:
        return self._since is not None and self._clock() - self._since >= self._quiet

    def interrupt(self) -> bool:
        return bool(self._busy() or self._game())
