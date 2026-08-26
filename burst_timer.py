"""
Pure burst-timing logic for the "only capture right after pressing B"
fix - separated from the actual keyboard hook and capture loop below,
since THIS logic (is a burst currently active, which burst is the
current one, when to reset the Mac Mini's history) can be genuinely
tested with an injectable fake clock, while a real global keyboard hook
can't be tested without an actual OS-level listener running.

Real-world fix #6, and a reversal of an earlier decision: pressing B
again now CANCELS the burst in progress and starts a clean new one,
rather than extending the old one. The original rule - a re-press while
still active is an "extension", not a fresh start, so it does NOT reset
the Mac Mini's reading history - was aimed at re-opening the SAME buy
menu mid-look. In practice the common case is the other one: buy, close
the menu, then open it again. Extending meant the pre-purchase readings
from the first look stayed in the consensus window alongside the
post-purchase ones, and since the consensus takes the MINIMUM, a
higher stale reading is harmless but a lower one is not - and the whole
point of the second look is that it is the more recent, more correct
one. Resetting on every press loses nothing: credits only ever go DOWN
within a buy phase, so the newest burst's readings are always the ones
worth keeping.

The generation counter exists for the same fix. Capture work is handed
to a thread pool (see agent.py's fix #5), and that pool's queue is
unbounded - so when a burst ends, whether by early exit or by being
cancelled, screenshots captured before it ended are still sitting in the
queue waiting to be sent. Stamping each one with the generation it was
captured in lets the worker drop stale work instead of POSTing readings
from a menu that is already closed.
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
        # Bumped by every trigger, so in-flight work from a burst that has
        # since been cancelled or ended can be identified and dropped. Starts
        # at 0, which no capture is ever stamped with - the first trigger
        # makes it 1 - so a stale 0 can never be mistaken for a live burst.
        self.generation = 0

    def trigger(self) -> None:
        """
        Called when B is pressed - CANCELS whatever burst was in progress
        and starts a clean new one from right now (fix #6 above). Every
        press is a fresh start, so every press resets the Mac Mini's
        reading history via consume_fresh_start(), and every press
        invalidates the previous generation's queued captures.
        """
        self.generation += 1
        self._fresh_start_pending = True
        self._active_until = self._clock() + self.duration_seconds

    def consume_fresh_start(self) -> bool:
        """Returns whether a trigger() has happened since this was last called, and clears the flag - a "read once" pattern so the history reset fires once per press, not once per capture tick."""
        was_fresh = self._fresh_start_pending
        self._fresh_start_pending = False
        return was_fresh

    def is_active(self) -> bool:
        return self._clock() < self._active_until

    def is_current(self, generation: int) -> bool:
        """
        Whether work captured in `generation` is still worth sending: the
        burst it belongs to must be both the newest one AND still running.
        Checked by the worker thread immediately before the network call,
        which is what stops a drained queue from POSTing readings taken
        after the buy menu closed.
        """
        return generation == self.generation and self.is_active()

    def force_end(self) -> None:
        """
        Ends the burst immediately, regardless of the configured duration -
        used for the early-exit-on-consecutive-failures optimization. Does
        NOT bump the generation: is_current() already fails on the
        is_active() half, and leaving the counter alone keeps "which burst
        is this" answering the same thing before and after an early exit.
        """
        self._active_until = self._clock()
