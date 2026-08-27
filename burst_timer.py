"""
Pure burst-timing logic for the "only capture right after pressing B"
fix - separated from the actual keyboard hook and capture loop below,
since THIS logic (is a burst currently active, which burst is the
current one, when to reset the Mac Mini's history) can be genuinely
tested with an injectable fake clock, while a real global keyboard hook
can't be tested without an actual OS-level listener running.

The generation counter exists because capture work is handed to a thread
pool (see agent.py's fix #5), and that pool's queue is unbounded - so
when a burst ends, screenshots captured before it ended are still
sitting in the queue waiting to be sent. Stamping each one with the
generation it was captured in lets the worker drop stale work instead of
POSTing readings from a menu that is already closed.

Real-world fix #10, and the one that finally matches how the buy menu is
actually driven. Two earlier rules were both wrong, in opposite
directions, and both for the same underlying reason: the agent sees only
the B key, and B is not the only way the menu opens and closes.

  Fix #6 said every press is a fresh start - open a new burst, reset the
  Mac Mini's history. That fired on the press that CLOSED the menu too,
  destroying the readings the buy phase had just produced.

  Fix #8 said B is a toggle, so a press during an active burst is the
  close of that burst. That is true of B in isolation, but the menu is
  closed with Esc at least as often, and the round starting closes it
  with no keystroke at all. Neither of those is visible here. So after an
  Esc, a press meant to RE-OPEN the menu landed while the burst was still
  nominally running and got treated as a close - the re-open was never
  captured at all. Reported exactly that way: closing the buy phase
  quickly and re-opening it sometimes produced no reading.

The rule now is the one that survives not knowing how the menu was
closed: **every press means "look at the menu now"**. Capturing starts
or restarts, unconditionally. If the press really was a B-toggle close,
the menu is gone, the next frames are unreadable, and the run of 422s
ends the burst early about a second and a half later - a small, bounded
cost. If the press was a genuine re-open, it is captured, which is the
case that was silently failing before.

That leaves the question fix #6 got wrong to be answered separately, and
it is now answered by the clock rather than by the keystroke. Resetting
the Mac Mini's reading history is only correct across a ROUND boundary;
re-opening the menu twice inside one buy phase must keep what the first
look read. A Valorant round essentially never completes in under twenty
seconds, so two presses closer together than that belong to the same buy
phase, and only a press that follows a gap of at least
NEW_ROUND_GAP_SECONDS begins a new one. trigger() returns which of the
two happened.

Within one buy phase the newest reading is the one that counts - you
have spent (or refunded) since the earlier ones - which the Mac Mini's
consensus now reflects directly; see credit_ocr.py's findings #7 and #8
on the other machine.

Real-world fix #11, from the same log that produced finding #8 there:
after the menu was closed the agent went on capturing for another
fifteen frames before the run of unreadable ones convinced it. Fix #10
accepted that as the price of not knowing how the menu was closed, and
for two of the three ways it is closed that is still true. But one of
them IS a keystroke, and this process simply was not listening for it.

Esc is now hooked alongside B and calls stop_capturing(). That answers
the question fix #10 could not - the menu is definitely gone - for the
common case, leaving the run of unreadable frames to cover only the two
cases with no keystroke at all: a B-toggle close, and the round starting
while the menu is still open.

stop_capturing() is deliberately NOT force_end(). The two differ over
work already sitting in the thread pool's queue, and that difference is
the whole reason this is a separate method. Everything queued when
force_end() fires was captured AFTER the menu closed, because that is
what the run of unreadable frames means, so it is discarded. Everything
queued when Esc arrives was captured BEFORE the menu closed - including,
in the log that prompted this, the single frame holding the value the
purchase had just produced - so it must still be sent. Discarding it
would throw away exactly the reading finding #8 exists to preserve.
"""

# Two presses closer together than this belong to the same buy phase, so
# the second must not wipe what the first read; a longer gap means a
# round went by in between and the old readings are a different round's
# budget. Twenty seconds is chosen against Valorant's own pacing: a round
# that has to be won, ended and followed by a fresh buy phase does not fit
# into it, while a shop-close-reshop inside one buy phase easily does.
#
# The Mac Mini enforces the same number from its own side as
# credit_ocr._READING_MAX_AGE_SECONDS, so a reset POST that never arrives
# (a network blip, an agent restart) cannot leave a previous round's
# readings standing forever. Change one and change the other.
NEW_ROUND_GAP_SECONDS = 20

import time as _time_module


class BurstTimer:
    """
    Tracks whether we're currently within a capture burst window.
    Pressing the trigger key starts (or restarts) the window; the capture
    loop checks is_active() on each tick to decide whether to actually
    capture right now.
    """

    def __init__(self, duration_seconds: float, clock=None, new_round_gap_seconds: float = NEW_ROUND_GAP_SECONDS):
        self.duration_seconds = duration_seconds
        self.new_round_gap_seconds = new_round_gap_seconds
        self._clock = clock or _time_module.time
        # Two windows, not one, because "should a new frame be grabbed"
        # and "is a frame that was already grabbed still worth sending"
        # stop being the same question the moment Esc is hooked (fix #11).
        # They move together everywhere except stop_capturing(), which
        # closes the first and leaves the second open so the queue drains.
        self._active_until: float = 0.0
        self._valid_until: float = 0.0
        # None rather than 0.0, so "no press has ever happened" is a state
        # of its own rather than "a press infinitely long ago" - the first
        # press must begin a new buy phase, and reading that off a
        # sentinel timestamp would depend on where the injected clock
        # happens to start.
        self._last_press_at: "float | None" = None
        self._fresh_start_pending = False
        # Bumped by every trigger, so in-flight work from a burst that has
        # since been superseded can be identified and dropped. Starts at 0,
        # which no capture is ever stamped with - the first trigger makes
        # it 1 - so a stale 0 can never be mistaken for a live burst.
        self.generation = 0

    def trigger(self) -> bool:
        """
        Called when B is pressed. Always starts or restarts capturing (see
        fix #10 above - a press can be an open or a close and this cannot
        tell, so it assumes the one whose cost when wrong is a second of
        unreadable frames rather than a whole missed buy look).

        Returns True if this press begins a NEW buy phase, meaning the Mac
        Mini's reading history should be reset, and False if it continues
        the buy phase already in progress, meaning the earlier readings
        must be kept.

        The generation is bumped either way. Frames captured before this
        press are from an older look at the menu, and the consensus on the
        other machine reports the most recent reading it holds, so letting
        a straggler arrive after this press could report a value from
        before the purchase that prompted the re-open.
        """
        now = self._clock()
        is_new_phase = self._last_press_at is None or (now - self._last_press_at) >= self.new_round_gap_seconds

        self._last_press_at = now
        self.generation += 1
        if is_new_phase:
            self._fresh_start_pending = True
        self._active_until = now + self.duration_seconds
        self._valid_until = self._active_until
        return is_new_phase

    def consume_fresh_start(self) -> bool:
        """Returns whether a trigger() that began a new buy phase has happened since this was last called, and clears the flag - a "read once" pattern so the history reset fires once per press, not once per capture tick."""
        was_fresh = self._fresh_start_pending
        self._fresh_start_pending = False
        return was_fresh

    def is_active(self) -> bool:
        return self._clock() < self._active_until

    def is_current(self, generation: int) -> bool:
        """
        Whether work captured in `generation` is still worth sending: the
        burst it belongs to must be the newest one, and must not have been
        invalidated by force_end() or simply expired. Checked by the worker
        thread immediately before the network call, which is what stops a
        drained queue from POSTing readings taken after the buy menu closed.

        Note this reads _valid_until, not is_active(). After Esc
        (stop_capturing(), fix #11) no new frames are grabbed but the ones
        already queued were all taken while the menu was open, and the last
        of them is the most valuable reading of the whole burst.
        """
        return generation == self.generation and self._clock() < self._valid_until

    def stop_capturing(self) -> bool:
        """
        Stops grabbing new frames, while leaving everything already queued
        valid to send. Called when Esc is pressed - the one close this
        process can actually observe (fix #11).

        Returns whether a burst was in fact running, so the caller can stay
        quiet about an Esc pressed for anything else. Esc opens Valorant's
        own menu when the buy menu is not up, and outside a burst there is
        nothing here for it to do.

        No new work can enter the queue after this, since the capture loop
        gates on is_active(), so the queue drains once and stays empty
        until the next press. That is why no drain deadline is needed:
        _valid_until is left where trigger() put it, and the next press
        bumps the generation, which invalidates any straggler anyway.
        """
        if not self.is_active():
            return False
        self._active_until = self._clock()
        return True

    def force_end(self) -> None:
        """
        Ends the burst immediately, regardless of the configured duration.

        One caller: the run of consecutive unreadable frames that means
        the menu closed without a keystroke this process could see - an
        Esc, or the round simply starting. Everything still queued when it
        fires was captured after the menu was already gone, so nothing is
        given a chance to drain; that is the difference between this and
        simply letting the window expire.

        Does NOT bump the generation: is_current() already stops matching
        once the burst is no longer valid, and leaving the counter alone
        keeps "which burst is this" answering the same thing before and
        after.

        Closes BOTH windows, which is the difference between this and
        stop_capturing() - see fix #11.
        """
        self._active_until = self._clock()
        self._valid_until = self._active_until
