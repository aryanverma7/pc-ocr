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

Real-world fix #8, and the thing fix #6 got wrong: in Valorant, B is a
TOGGLE. The same key that opens the buy menu is the key most people
close it with. Fix #6's "every press is a fresh start" rule therefore
fired on the close as well as the open - so the press that ended a buy
phase wiped the Mac Mini's reading history and started a fresh burst
pointed at a menu that was already gone. The readings from the look you
just took were destroyed by the act of finishing it, the new burst read
nothing but gameplay, and the dashboard showed "No reading yet" for a
buy phase that had in fact been read perfectly. Reported exactly that
way: the agent printed real numbers and the panel stayed empty.

trigger() now models the toggle it actually is. A press while no burst
is running is an OPEN: new generation, history reset, full window. A
press while one IS running is a CLOSE: the burst ends and the readings
stand. The return value says which happened so the caller doesn't have
to re-derive it.

A close cannot simply drop everything in flight, though, the way an
early exit can. An early exit only fires after a long run of unreadable
frames, so what it discards is known garbage; a close discards the
frames taken in the final moments before the menu shut, which are the
single most valuable ones in the burst - "min next round" is at its
truest right before you stop shopping. So ending on a close keeps a
short send-grace during which already-captured work may still be POSTed
even though capturing itself has stopped. The two questions "should the
loop capture right now" and "is this queued frame still worth sending"
were the same question before this; they are not.
"""

# How long queued captures stay sendable after the buy menu is closed with
# a B press. Long enough to cover the pool's backlog at the real capture
# rate, short enough that nothing captured after the menu shut can slip in
# - the frames it lets through were all grabbed BEFORE the close.
SEND_GRACE_SECONDS = 0.75
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
        # Separate from _active_until on purpose (fix #8): capturing stops
        # the instant the menu closes, but frames already grabbed stay
        # worth sending for a moment longer.
        self._send_until: float = 0.0
        self._fresh_start_pending = False
        # Bumped by every trigger, so in-flight work from a burst that has
        # since been cancelled or ended can be identified and dropped. Starts
        # at 0, which no capture is ever stamped with - the first trigger
        # makes it 1 - so a stale 0 can never be mistaken for a live burst.
        self.generation = 0

    def trigger(self) -> bool:
        """
        Called when B is pressed. Returns True if this press OPENED the buy
        menu and started a burst, False if it CLOSED one that was already
        running.

        B is a toggle in game and is a toggle here (fix #8 above). Pressing
        it with no burst running is an open: a new generation, a fresh
        start flag so the Mac Mini's history gets reset, and the full
        window. Pressing it with a burst running is the close of that same
        look - the burst ends, the history is left exactly as it is, and
        no new burst begins.
        """
        if self.is_active():
            self.force_end(send_grace_seconds=SEND_GRACE_SECONDS)
            return False

        self.generation += 1
        self._fresh_start_pending = True
        self._active_until = self._clock() + self.duration_seconds
        # Cleared rather than left over from a previous close, so a grace
        # granted to an older burst can never outlive this one.
        self._send_until = 0.0
        return True

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
        return generation == self.generation and self._clock() < max(self._active_until, self._send_until)

    def force_end(self, send_grace_seconds: float = 0.0) -> None:
        """
        Ends the burst immediately, regardless of the configured duration.

        Two callers with genuinely different needs. The early exit on
        consecutive failures passes no grace: it only fires after a long
        run of unreadable frames, so everything still queued behind it was
        captured after the menu was already gone. A close by B press passes
        SEND_GRACE_SECONDS, because what is queued there was captured
        while the menu was open and holds the most accurate reading of the
        whole burst.

        Does NOT bump the generation: is_current() already stops matching
        once both deadlines pass, and leaving the counter alone keeps
        "which burst is this" answering the same thing before and after.
        """
        now = self._clock()
        self._active_until = now
        self._send_until = now + send_grace_seconds
