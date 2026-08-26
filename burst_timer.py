"""
Pure burst-timing logic for the "only capture right after pressing B"
fix - separated from the actual keyboard hook and capture loop below,
since THIS logic (is a burst currently active, does pressing B again
extend it, was this trigger a fresh start vs an extension) can be
genuinely tested with an injectable fake clock, while a real global
keyboard hook can't be tested without an actual OS-level listener
running.
"""
import time as _time_module


class BurstTimer:
    """
    Tracks whether we're currently within a capture burst window.
    Pressing the trigger key starts (or extends) the window; the capture
    loop checks is_active() on each tick to decide whether to actually
    capture right now.
    """

    def __init__(self, duration_seconds: float, clock=None):
        self.duration_seconds = duration_seconds
        self._clock = clock or _time_module.time
        self._active_until: float = 0.0
        self._fresh_start_pending = False

    def trigger(self) -> None:
        """
        Called when B is pressed - starts or extends the active window
        from right now. If this trigger comes while NOT already active
        (a genuinely new buy phase, not just re-opening the same one),
        marks a fresh-start flag - consumed once via
        consume_fresh_start() to know when to reset the Mac Mini's
        reading history, since a real bug showed the previous round's
        readings otherwise bleed into the new round's consensus.
        """
        if not self.is_active():
            self._fresh_start_pending = True
        self._active_until = self._clock() + self.duration_seconds

    def consume_fresh_start(self) -> bool:
        """Returns whether the most recent trigger() was a fresh start (not an extension), and clears the flag - a "read once" pattern so this only fires once per genuinely new burst."""
        was_fresh = self._fresh_start_pending
        self._fresh_start_pending = False
        return was_fresh

    def is_active(self) -> bool:
        return self._clock() < self._active_until

    def force_end(self) -> None:
        """Ends the burst immediately, regardless of the configured duration - used for the early-exit-on-consecutive-failures optimization."""
        self._active_until = self._clock()
