from burst_timer import BurstTimer, SEND_GRACE_SECONDS


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
        The real-world scenario: open the buy menu, close it, open it again
        a few seconds later. The second open gets its own full window
        rather than inheriting whatever was left of the first one's.

        Note the three presses - open, close, open. B is a toggle, so
        that is what re-opening actually looks like from here.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=6, clock=clock)
        timer.trigger()   # opened
        clock.advance(2)
        timer.trigger()   # closed
        clock.advance(2)
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
    def test_the_very_first_trigger_is_a_fresh_start(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        assert timer.consume_fresh_start() is True

    def test_a_press_that_CLOSES_the_menu_is_not_a_fresh_start(self):
        """
        The reported bug, at its source (fix #8). B is a toggle, so the
        press that ends a buy phase is the same keystroke as the one that
        began it. Treating it as a fresh start reset the Mac Mini's reading
        history at the exact moment the readings had just become correct,
        and the dashboard showed "No reading yet" for a phase that had been
        read perfectly.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        timer.consume_fresh_start()  # consume the open
        clock.advance(5)  # still well within the 20s window
        timer.trigger()  # B pressed again - this CLOSES the menu
        assert timer.consume_fresh_start() is False

    def test_a_reopen_after_a_close_IS_a_fresh_start(self):
        """
        The other side of the same rule: once the burst has ended, the next
        press is genuinely an open again and must reset the history - it
        may well be a different round by then.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        timer.consume_fresh_start()
        clock.advance(5)
        timer.trigger()   # close
        timer.consume_fresh_start()
        clock.advance(5)
        timer.trigger()   # open again
        assert timer.consume_fresh_start() is True

    def test_triggering_again_after_the_window_expired_IS_a_fresh_start(self):
        """
        The actual real bug this fixes: if the burst fully ended (the
        previous buy phase is genuinely over) and a new one starts later,
        that IS a new round and must reset the history.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        timer.consume_fresh_start()
        clock.advance(25)  # past the 20s window - fully expired
        timer.trigger()  # a new buy phase, later
        assert timer.consume_fresh_start() is True

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

    def test_each_OPENING_press_advances_the_generation(self):
        """A close doesn't - there's no new burst for a new number to name."""
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()          # open
        assert timer.generation == 1
        clock.advance(1)
        timer.trigger()          # close
        assert timer.generation == 1
        clock.advance(1)
        timer.trigger()          # open again
        assert timer.generation == 2

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

    def test_a_new_OPENING_press_invalidates_the_previous_bursts_queued_work(self):
        """B pressed for a new buy phase must cancel what was in flight, not merge with it."""
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()          # open, generation 1
        clock.advance(3)
        timer.trigger()          # close
        clock.advance(SEND_GRACE_SECONDS + 1)  # let generation 1's grace run out
        timer.trigger()          # open, generation 2
        assert timer.is_current(1) is False   # captured under the finished burst
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


class TestForceEnd:
    def test_force_end_makes_the_timer_immediately_inactive(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        assert timer.is_active() is True

        timer.force_end()

        assert timer.is_active() is False

    def test_force_end_before_the_full_duration_elapsed_still_ends_it_early(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(4)  # only 4 of 20 seconds have passed
        timer.force_end()
        assert timer.is_active() is False


class TestToggleSemantics:
    """
    Fix #8. In Valorant, B opens the buy menu and B closes it - one key,
    two meanings, and the agent only ever sees "B was pressed". Modelling
    that as a toggle is what stops the closing press from being read as
    the start of another buy phase.
    """

    def test_the_first_press_reports_that_it_opened_a_burst(self):
        timer = BurstTimer(duration_seconds=20, clock=FakeClock())
        assert timer.trigger() is True

    def test_a_press_during_an_active_burst_reports_that_it_closed_one(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(3)
        assert timer.trigger() is False

    def test_a_closing_press_stops_the_capturing(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(3)
        timer.trigger()
        assert timer.is_active() is False

    def test_a_press_after_the_window_expired_opens_rather_than_closes(self):
        """
        Nothing is running by then, so there is nothing to close - and this
        is the ordinary case of a new round.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(21)
        assert timer.trigger() is True

    def test_a_press_after_an_early_exit_opens_rather_than_closes(self):
        """
        The Esc case: the menu was closed some other way, the run of
        unreadable frames ended the burst, and the next B is a real open.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(3)
        timer.force_end()
        assert timer.trigger() is True


class TestSendGrace:
    """
    Closing the menu ends the capturing immediately but must not throw away
    what is already queued: those frames were grabbed while the menu was
    still open, and they hold the lowest - which is to say the truest -
    "min next round" of the whole burst.
    """

    def test_queued_work_from_before_a_close_is_still_worth_sending(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(3)
        timer.trigger()  # closed
        assert timer.is_active() is False       # nothing new gets captured
        assert timer.is_current(1) is True      # but the backlog still goes out

    def test_the_grace_does_expire(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(3)
        timer.trigger()
        clock.advance(SEND_GRACE_SECONDS + 0.01)
        assert timer.is_current(1) is False

    def test_an_early_exit_gets_no_grace_at_all(self):
        """
        The deliberate asymmetry. An early exit only fires after a long run
        of frames that read as nothing, so everything behind it is known
        garbage - the opposite of what a close leaves queued.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(3)
        timer.force_end()
        assert timer.is_current(1) is False

    def test_a_new_burst_does_not_inherit_the_previous_ones_grace(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        timer.trigger()          # close, grace granted to generation 1
        timer.trigger()          # open, generation 2
        timer.force_end()        # early exit, no grace
        assert timer.is_current(2) is False
