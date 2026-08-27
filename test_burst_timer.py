from burst_timer import BurstTimer, NEW_ROUND_GAP_SECONDS


class FakeClock:
    """Lets tests control time exactly, rather than depending on real sleeps."""
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestBurstTimer:
    def test_not_active_before_being_triggered_at_all(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=6, clock=clock)
        assert timer.is_active() is False

    def test_active_immediately_after_being_triggered(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=6, clock=clock)
        timer.trigger()
        assert timer.is_active() is True

    def test_still_active_partway_through_the_window(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=6, clock=clock)
        timer.trigger()
        clock.advance(5)
        assert timer.is_active() is True

    def test_no_longer_active_after_the_window_fully_elapses(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=6, clock=clock)
        timer.trigger()
        clock.advance(6.1)
        assert timer.is_active() is False

    def test_exactly_at_the_boundary_is_no_longer_active(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=6, clock=clock)
        timer.trigger()
        clock.advance(6)
        assert timer.is_active() is False

    def test_reopening_after_a_close_restarts_the_full_window_from_that_press(self):
        """
        The real-world scenario: open the buy menu, close it with Esc, open
        it again a few seconds later. Only the two B presses are visible
        here - the Esc is not - and the second press gets its own full
        window rather than inheriting whatever was left of the first one's.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=6, clock=clock)
        timer.trigger()   # opened
        clock.advance(4)  # shopped, then closed with Esc - unseen from here
        timer.trigger()   # opened again
        clock.advance(5)  # 9s after the first press, past its own window, inside the new one
        assert timer.is_active() is True

    def test_a_stale_earlier_trigger_does_not_reactivate_after_a_newer_one_expires(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=6, clock=clock)
        timer.trigger()
        clock.advance(6.1)
        assert timer.is_active() is False
        # Confirms expiry is real, not a fluke - staying inactive as time keeps moving forward
        clock.advance(10)
        assert timer.is_active() is False


class TestFreshStartDetection:
    """
    Fix #10. Whether to reset the Mac Mini's reading history is decided by
    how long it has been since the last press, not by what the press was
    taken to mean. Two presses inside NEW_ROUND_GAP_SECONDS are two looks
    at one buy phase and must keep what the first look read; a longer gap
    means a round went by in between.
    """

    def test_the_very_first_trigger_is_a_fresh_start(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)
        timer.trigger()
        assert timer.consume_fresh_start() is True

    def test_a_second_look_at_the_same_buy_phase_is_not_a_fresh_start(self):
        """
        The reported behaviour, stated as a rule: open the buy menu, buy a
        rifle, close it, re-open it to add armour. Resetting on that second
        press throws away everything the first look read, for a phase that
        had been read perfectly.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)
        timer.trigger()
        timer.consume_fresh_start()
        clock.advance(8)  # comfortably inside one buy phase
        timer.trigger()
        assert timer.consume_fresh_start() is False

    def test_a_second_look_is_kept_even_after_the_first_burst_already_ended(self):
        """
        The case fix #8 could not express, and the one that actually bit.
        Closing with Esc ends the burst via the early exit, so by the time
        the menu is re-opened nothing is running - but it is still the same
        buy phase, and the gap, not the burst, is what says so.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)
        timer.trigger()
        timer.consume_fresh_start()
        clock.advance(2)
        timer.force_end()          # Esc, then the run of unreadable frames
        clock.advance(6)
        timer.trigger()            # re-opened, same buy phase
        assert timer.consume_fresh_start() is False

    def test_a_press_after_the_gap_IS_a_fresh_start(self):
        """
        The next round. Its readings are a different budget entirely, and
        leaving the previous round's in the window is the contamination
        this whole mechanism exists to prevent.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)
        timer.trigger()
        timer.consume_fresh_start()
        clock.advance(20)
        timer.trigger()
        assert timer.consume_fresh_start() is True

    def test_exactly_at_the_gap_counts_as_a_new_round(self):
        """A boundary has to fall on one side; it falls on the safe one - resetting costs a stale window, not a wrong answer."""
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)
        timer.trigger()
        timer.consume_fresh_start()
        clock.advance(19.99)
        timer.trigger()
        assert timer.consume_fresh_start() is False

    def test_the_gap_is_measured_from_the_last_press_not_the_first(self):
        """
        Three looks at one buy phase, each less than the gap after the one
        before it, spanning more than the gap in total. Every one of them
        continues the same phase.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)
        timer.trigger()
        timer.consume_fresh_start()
        for _ in range(3):
            clock.advance(15)
            timer.trigger()
            assert timer.consume_fresh_start() is False

    def test_the_gap_defaults_to_the_module_constant(self):
        """
        The default has to match what the Mac Mini enforces from its own
        side (credit_ocr._READING_MAX_AGE_SECONDS), so it is not left to
        whatever a caller happens to pass.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock)
        assert timer.new_round_gap_seconds == NEW_ROUND_GAP_SECONDS
        timer.trigger()
        timer.consume_fresh_start()
        clock.advance(NEW_ROUND_GAP_SECONDS - 0.5)
        timer.trigger()
        assert timer.consume_fresh_start() is False

    def test_consuming_the_flag_clears_it_so_it_does_not_fire_twice(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        assert timer.consume_fresh_start() is True
        assert timer.consume_fresh_start() is False  # already consumed once


class TestGenerationTracking:
    """
    The mechanism that stops a drained thread-pool queue from POSTing
    captures taken after the buy menu closed - agent.py's fix #6.
    """

    def test_generation_starts_at_zero_which_no_capture_is_ever_stamped_with(self):
        timer = BurstTimer(duration_seconds=20, clock=FakeClock())
        assert timer.generation == 0
        assert timer.is_current(0) is False

    def test_every_press_advances_the_generation(self):
        """
        Including a press that continues the same buy phase. Frames from
        before it were captured at an earlier look, and the Mac Mini now
        reports the most recent reading it holds - so a straggler arriving
        after the re-open could report a balance from before the purchase
        that prompted it.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)
        timer.trigger()
        assert timer.generation == 1
        clock.advance(1)
        timer.trigger()          # same buy phase, second look
        assert timer.generation == 2
        clock.advance(30)
        timer.trigger()          # a new round
        assert timer.generation == 3

    def test_work_from_the_live_burst_is_still_worth_sending(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(3)
        assert timer.is_current(1) is True

    def test_the_exact_reported_bug_queued_work_after_an_early_exit_is_dropped(self):
        """
        The real log: ten 422s trip the early exit, then nine MORE 422s
        print. Those nine were screenshots already sitting in the pool's
        unbounded queue. force_end() stops the loop capturing but cannot
        unqueue them - this check is what makes the worker drop them
        instead of POSTing readings from a closed menu.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(3)
        timer.force_end()
        assert timer.is_current(1) is False

    def test_a_new_press_invalidates_the_previous_bursts_queued_work(self):
        """B pressed again must cancel what was in flight, not merge with it."""
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)
        timer.trigger()          # generation 1
        clock.advance(3)
        timer.trigger()          # generation 2 - a second look at the same phase
        assert timer.is_current(1) is False   # captured under the superseded burst
        assert timer.is_current(2) is True    # captured under the live one

    def test_work_is_dropped_once_the_window_simply_expires_too(self):
        """No early exit, no re-press - a burst that just runs out has the same problem."""
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(21)
        assert timer.is_current(1) is False

    def test_force_end_leaves_the_generation_alone(self):
        """
        is_current() already fails on its is_active() half after an early
        exit, so bumping here would only make "which burst is this" answer
        differently before and after an early exit for no gain.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        timer.force_end()
        assert timer.generation == 1


class TestEveryPressCaptures:
    """
    Fix #10, and a reversal of fix #8. The menu is opened with B, closed
    with Esc, and closed by the round starting when a purchase is made at
    the last second - only the first of those is a keystroke this process
    sees. So a press cannot be classified as an open or a close, and the
    rule that survives not knowing is to capture on every one of them.

    trigger()'s return value no longer says open-or-close. It says whether
    this press begins a new buy phase, which is a question about the clock.
    """

    def test_the_first_press_starts_capturing(self):
        timer = BurstTimer(duration_seconds=30, clock=FakeClock())
        timer.trigger()
        assert timer.is_active() is True

    def test_a_press_during_an_active_burst_keeps_capturing(self):
        """
        The bug fix #8 introduced: after an Esc, a press meant to RE-OPEN
        the menu arrived while the burst was still nominally running, was
        read as a close, and the whole second look went uncaptured.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)
        timer.trigger()
        clock.advance(3)
        timer.trigger()
        assert timer.is_active() is True

    def test_a_press_during_an_active_burst_restarts_the_full_window(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)
        timer.trigger()
        clock.advance(25)
        timer.trigger()          # 25s in, 5s of the original window left
        clock.advance(20)        # 45s after the first press
        assert timer.is_active() is True

    def test_a_press_after_an_early_exit_starts_capturing_again(self):
        """
        The Esc case end to end: the menu was closed some other way, the
        run of unreadable frames ended the burst, and the next B press is a
        real re-open that has to be captured.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)
        timer.trigger()
        clock.advance(3)
        timer.force_end()
        assert timer.is_active() is False
        clock.advance(4)
        timer.trigger()
        assert timer.is_active() is True

    def test_a_press_reports_whether_it_began_a_new_buy_phase(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)
        assert timer.trigger() is True     # first ever press
        clock.advance(5)
        assert timer.trigger() is False    # same buy phase
        clock.advance(45)
        assert timer.trigger() is True     # next round


class TestTheReportedRoundEndToEnd:
    """
    The scenarios as described, replayed against the timer alone. Each one
    asserts the two things that decide whether a reading survives: is the
    agent capturing, and is the Mac Mini's history about to be wiped.
    """

    def test_open_with_B_close_with_Esc_reopen_to_buy_more(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)

        timer.trigger()                              # B - buy phase starts
        assert timer.consume_fresh_start() is True   # a genuinely new round: reset
        clock.advance(3)
        timer.force_end()                            # Esc, then the 422 run

        clock.advance(5)
        timer.trigger()                              # B again - add armour
        assert timer.is_active() is True             # the second look IS captured
        assert timer.consume_fresh_start() is False  # and the first look's readings stand

    def test_bought_at_the_last_second_and_the_round_closed_the_menu(self):
        """
        The edge case as reported: no Esc at all, the round simply starts.
        Nothing at all is pressed, so nothing can go wrong here by
        mistake - the burst ends on the unreadable frames that follow, and
        the readings taken while the menu was open are left alone.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=30, clock=clock, new_round_gap_seconds=20)

        timer.trigger()
        timer.consume_fresh_start()
        clock.advance(28)
        timer.force_end()                            # the round started; frames stop reading

        assert timer.is_active() is False
        assert timer.consume_fresh_start() is False  # nothing was reset on the way out

        clock.advance(45)                            # play out the round
        timer.trigger()                              # next round's buy menu
        assert timer.consume_fresh_start() is True   # now, and only now, reset


class TestForceEndIsTheOnlyEarlyStop:
    def test_force_end_makes_the_timer_immediately_inactive(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        assert timer.is_active() is True
        timer.force_end()
        assert timer.is_active() is False

    def test_force_end_drops_queued_work_with_no_grace_period(self):
        """
        The deliberate asymmetry, and the reason force_end takes no
        arguments. It only fires after a long run of frames that read as
        nothing, so everything queued behind it was captured after the menu
        was already gone - known garbage, not a last good reading.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(3)
        timer.force_end()
        assert timer.is_current(1) is False

    def test_a_press_after_a_force_end_is_still_capturing_normally(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        timer.force_end()
        timer.trigger()
        assert timer.is_active() is True
        assert timer.is_current(2) is True
