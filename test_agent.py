from unittest.mock import patch, MagicMock
from PIL import Image

import agent


def make_config(**overrides):
    config = {
        "backend_url": "https://hub.dualbladex.org/api/ocr/credit-report",
        "agent_secret": "test-secret-123",
        "burst_duration_seconds": 6,
        "region": {"left": 10, "top": 20, "width": 100, "height": 30},
    }
    config.update(overrides)
    return config


class TestCaptureAndSend:
    @patch("agent.requests.post")
    @patch("agent.ImageGrab.grab")
    def test_sends_the_cropped_region_to_the_correct_url_with_the_secret_header(self, mock_grab, mock_post):
        mock_grab.return_value = Image.new("RGB", (1920, 1080))
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"credits": 4900})

        agent.capture_and_send(make_config())

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args.args[0] == "https://hub.dualbladex.org/api/ocr/credit-report"
        assert call_args.kwargs["headers"]["X-Agent-Secret"] == "test-secret-123"

    @patch("agent.requests.post")
    @patch("agent.ImageGrab.grab")
    def test_sends_a_correctly_sized_crop_matching_the_calibrated_region(self, mock_grab, mock_post):
        mock_grab.return_value = Image.new("RGB", (1920, 1080))
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"credits": 4900})

        agent.capture_and_send(make_config(region={"left": 0, "top": 0, "width": 150, "height": 45}))

        sent_bytes = mock_post.call_args.kwargs["data"]
        # Confirms the actual PNG bytes sent decode back to the exact
        # calibrated dimensions, not just that SOME data was sent.
        from io import BytesIO
        sent_image = Image.open(BytesIO(sent_bytes))
        assert sent_image.size == (150, 45)

    @patch("agent.requests.post")
    @patch("agent.ImageGrab.grab")
    def test_an_already_cropped_image_is_sent_as_is_without_grabbing_again(self, mock_grab, mock_post):
        """
        The main loop crops on its own timing tick and hands the crop to a
        worker (fix #5/#6), so this must neither re-grab nor re-crop - a
        second crop would slice a region-sized offset out of an already
        region-sized image.
        """
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"credits": 4900})
        already_cropped = Image.new("RGB", (150, 45))

        agent.capture_and_send(make_config(), cropped=already_cropped)

        mock_grab.assert_not_called()
        from io import BytesIO
        sent_image = Image.open(BytesIO(mock_post.call_args.kwargs["data"]))
        assert sent_image.size == (150, 45)

    @patch("agent.requests.post")
    @patch("agent.ImageGrab.grab")
    def test_handles_a_network_failure_without_crashing(self, mock_grab, mock_post):
        import requests
        mock_grab.return_value = Image.new("RGB", (1920, 1080))
        mock_post.side_effect = requests.exceptions.ConnectionError("could not connect")

        agent.capture_and_send(make_config())  # must not raise

    @patch("agent.requests.post")
    @patch("agent.ImageGrab.grab")
    def test_handles_each_real_backend_status_code_without_crashing(self, mock_grab, mock_post, capsys):
        mock_grab.return_value = Image.new("RGB", (1920, 1080))

        for status in (200, 401, 422, 503, 418):
            mock_post.return_value = MagicMock(
                status_code=status,
                json=lambda: {"credits": 4900},
                text="some error text",
            )
            agent.capture_and_send(make_config())  # must not raise for any real status code

        output = capsys.readouterr().out
        assert "401" in output
        assert "422" in output
        assert "503" in output


class TestConsecutiveFailureTracker:
    # Read from the module rather than hardcoded, so retuning the real
    # threshold can't leave these tests quietly asserting the old value.
    THRESHOLD = agent.CONSECUTIVE_FAILURES_BEFORE_EARLY_EXIT

    def test_does_not_exit_early_before_reaching_the_threshold(self):
        tracker = agent.ConsecutiveFailureTracker(threshold=self.THRESHOLD)
        for _ in range(self.THRESHOLD - 1):
            tracker.record_status(422)
        assert tracker.should_early_exit() is False

    def test_exits_early_once_the_threshold_is_reached(self):
        tracker = agent.ConsecutiveFailureTracker(threshold=self.THRESHOLD)
        for _ in range(self.THRESHOLD):
            tracker.record_status(422)
        assert tracker.should_early_exit() is True

    def test_a_single_success_resets_the_count_back_to_zero(self):
        """
        The actual real scenario this guards against: a brief, transient
        misread mid-shopping (still a genuinely open buy menu) must not
        count toward "the menu has closed" - only a genuinely CONSECUTIVE
        run of failures should.
        """
        tracker = agent.ConsecutiveFailureTracker(threshold=self.THRESHOLD)
        for _ in range(self.THRESHOLD - 1):
            tracker.record_status(422)
        tracker.record_status(200)  # one success resets the streak
        for _ in range(self.THRESHOLD - 1):
            tracker.record_status(422)
        assert tracker.should_early_exit() is False  # still short of a full run

    def test_unrelated_statuses_do_not_affect_the_count_either_way(self):
        """
        401/503/network failures are different problems entirely - they
        shouldn't accidentally build toward the "menu closed" threshold,
        nor should they reset a genuine 422 streak.
        """
        tracker = agent.ConsecutiveFailureTracker(threshold=self.THRESHOLD)
        before = self.THRESHOLD // 2
        after = self.THRESHOLD - before
        for _ in range(before):
            tracker.record_status(422)
        tracker.record_status(401)
        tracker.record_status(None)  # network error
        tracker.record_status(503)
        for _ in range(after):
            tracker.record_status(422)
        assert tracker.should_early_exit() is True  # the streak spans the interruptions

    def test_reset_clears_the_count(self):
        tracker = agent.ConsecutiveFailureTracker(threshold=self.THRESHOLD)
        for _ in range(self.THRESHOLD):
            tracker.record_status(422)
        assert tracker.should_early_exit() is True
        tracker.reset()
        assert tracker.should_early_exit() is False

    def test_the_threshold_is_ten_readings_about_2_5_seconds_at_the_real_capture_rate(self):
        """
        Pinned deliberately. Like the Mac Mini's consensus window, this is
        a duration expressed in readings - it only means "2.5 seconds"
        while CAPTURE_INTERVAL_WITHIN_BURST stays at 0.25s, so the two
        have to be retuned together.
        """
        assert agent.CONSECUTIVE_FAILURES_BEFORE_EARLY_EXIT == 10
        assert agent.CAPTURE_INTERVAL_WITHIN_BURST == 0.25
        seconds_before_early_exit = (
            agent.CONSECUTIVE_FAILURES_BEFORE_EARLY_EXIT * agent.CAPTURE_INTERVAL_WITHIN_BURST
        )
        assert seconds_before_early_exit == 2.5


class TestResetHistory:
    @patch("agent.requests.post")
    def test_posts_to_the_correct_reset_url_derived_from_the_backend_url(self, mock_post):
        config = make_config(backend_url="https://hub.dualbladex.org/api/ocr/credit-report")
        agent.reset_history(config)

        mock_post.assert_called_once()
        called_url = mock_post.call_args.args[0]
        assert called_url == "https://hub.dualbladex.org/api/ocr/reset"

    @patch("agent.requests.post")
    def test_sends_the_correct_secret_header(self, mock_post):
        config = make_config(agent_secret="my-real-secret")
        agent.reset_history(config)

        assert mock_post.call_args.kwargs["headers"]["X-Agent-Secret"] == "my-real-secret"

    @patch("agent.requests.post")
    def test_handles_a_network_failure_without_crashing(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError("could not connect")

        agent.reset_history(make_config())  # must not raise


class _StopAfter:
    """
    A stand-in for threading.Event that reports "stopped" after a fixed
    number of checks, so heartbeat_loop's own while condition ends the
    loop exactly the way a real stop does - rather than the test breaking
    out of it some other way and proving less than it looks like it does.
    """

    def __init__(self, checks: int):
        self._remaining = checks

    def is_set(self) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False


class TestHeartbeat:
    @patch("agent.requests.post")
    def test_pings_the_heartbeat_endpoint_beside_the_configured_url(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)

        agent.send_heartbeat(make_config())

        assert mock_post.call_args.args[0] == "https://hub.dualbladex.org/api/ocr/heartbeat"
        assert mock_post.call_args.kwargs["headers"]["X-Agent-Secret"] == "test-secret-123"

    @patch("agent.requests.post")
    def test_a_network_failure_comes_back_as_none_rather_than_raising(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError("no route to host")

        assert agent.send_heartbeat(make_config()) is None

    def test_prints_once_per_change_of_state_not_once_per_ping(self):
        # Five identical answers must produce exactly one line. At one ping
        # every 15 seconds, printing per attempt would bury the capture
        # output that is actually worth reading during a stream.
        printed = []
        with patch("builtins.print", side_effect=lambda *a: printed.append(" ".join(str(x) for x in a))):
            agent.heartbeat_loop(
                make_config(),
                _StopAfter(5),
                sender=lambda cfg: 200,
                sleep=lambda seconds: None,
            )

        assert len(printed) == 1
        assert "can see this agent" in printed[0]

    def test_prints_again_when_the_link_goes_down_and_when_it_returns(self):
        answers = iter([200, 200, None, None, 200])
        printed = []
        with patch("builtins.print", side_effect=lambda *a: printed.append(" ".join(str(x) for x in a))):
            agent.heartbeat_loop(
                make_config(),
                _StopAfter(5),
                sender=lambda cfg: next(answers),
                sleep=lambda seconds: None,
            )

        assert len(printed) == 3
        assert "can't reach the Mac Mini" in printed[1]
        assert "can see this agent" in printed[2]

    def test_a_401_names_the_secret_mismatch_rather_than_a_generic_failure(self):
        printed = []
        with patch("builtins.print", side_effect=lambda *a: printed.append(" ".join(str(x) for x in a))):
            agent.heartbeat_loop(
                make_config(),
                _StopAfter(1),
                sender=lambda cfg: 401,
                sleep=lambda seconds: None,
            )

        assert "agent_secret" in printed[0]

    def test_a_404_points_at_an_out_of_date_backend(self):
        # The specific case of pulling this agent before pulling the Mac
        # Mini: the route simply doesn't exist there yet.
        printed = []
        with patch("builtins.print", side_effect=lambda *a: printed.append(" ".join(str(x) for x in a))):
            agent.heartbeat_loop(
                make_config(),
                _StopAfter(1),
                sender=lambda cfg: 404,
                sleep=lambda seconds: None,
            )

        assert "predates the heartbeat route" in printed[0]

    def test_the_interval_fits_three_times_inside_the_mac_minis_cutoff(self):
        # Guards the pairing described beside the constant: the Mac Mini's
        # ocr_agent.HEARTBEAT_TIMEOUT_SECONDS is 45, and this interval has
        # to leave room for two dropped pings before the dashboard reports
        # the agent dead.
        assert agent.HEARTBEAT_INTERVAL_SECONDS * 3 <= 45
