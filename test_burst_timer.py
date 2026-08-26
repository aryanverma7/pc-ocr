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

    def test_re_triggering_while_still_active_is_NOT_a_fresh_start(self):
        """
        The critical distinction: pressing B again to extend an
        already-open buy menu is the SAME buy phase, not a new one - it
        must not falsely signal a history reset.
        """
        clock = FakeClock()
        timer = BurstTimer(duration_seconds=20, clock=clock)
        timer.trigger()
        timer.consume_fresh_start()  # consume the first, genuine fresh start
        clock.advance(5)  # still well within the 20s window
        timer.trigger()  # re-press while already active
        assert timer.consume_fresh_start() is False

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
