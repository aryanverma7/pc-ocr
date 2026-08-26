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
