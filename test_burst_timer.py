from burst_timer import BurstTimer


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

    def test_pressing_the_trigger_again_extends_the_window_from_now(self):
        """
        The actual real-world scenario this exists for: opening the buy
        menu, closing it, then reopening it a few seconds later should
        restart the full window from that second press, not let it expire
        based on the first press alone.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=6, clock=clock)
        timer.trigger()
        clock.advance(4)  # partway through the first window
        timer.trigger()   # B pressed again
        clock.advance(4)  # would be past the FIRST window's end (4+4=8 > 6), but not the second's
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

    def test_re_triggering_while_still_active_IS_a_fresh_start_too(self):
        """
        Deliberately the reverse of what this test asserted before - see
        burst_timer.py's fix #6. The old rule treated a re-press during an
        active burst as extending the same buy phase, so it did NOT reset
        the Mac Mini's history. The real pattern turned out to be buy,
        close, re-open, which left the pre-purchase readings sitting in the
        consensus window next to the post-purchase ones. Credits only ever
        go down within a buy phase, so the newest burst's readings are
        always the ones worth keeping and resetting costs nothing.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        timer.consume_fresh_start()  # consume the first one
        clock.advance(5)  # still well within the 20s window
        timer.trigger()  # re-press while already active
        assert timer.consume_fresh_start() is True

    def test_re_triggering_restarts_the_full_window_rather_than_leaving_it_where_it_was(self):
        """A cancel-and-restart, not just a flag change - the new burst gets its whole duration."""
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(19)
        timer.trigger()
        clock.advance(19)  # 38s after the first press, well past its own window
        assert timer.is_active() is True

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

    def test_each_press_advances_the_generation(self):
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        assert timer.generation == 1
        clock.advance(1)
        timer.trigger()
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

    def test_a_new_press_invalidates_the_previous_bursts_queued_work(self):
        """The other half of the report: B pressed again must cancel what was in flight, not merge with it."""
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        clock.advance(3)
        timer.trigger()
        assert timer.is_current(1) is False   # captured under the cancelled burst
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
