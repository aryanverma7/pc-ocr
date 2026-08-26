"""
The actual running agent (Task #8's gaming-PC side) - captures the
calibrated region only in a burst right after pressing B (opening
Valorant's buy menu), and sends it to the Mac Mini for OCR.

Real-world fix #1, not a design guess: this used to run on a plain timer,
continuously, regardless of whether the buy menu was even open. During
actual gameplay the calibrated screen region often shows something
completely different (like the minimap), which Tesseract would then
misread as a number - garbage readings like "113" with nothing wrong on
the Mac Mini side at all. Only capturing right after B is pressed means
OCR only ever runs while the buy menu is genuinely open.

Real-world fix #2: the burst duration should cover however long you
actually take to shop (confirmed with your own play pattern - see
region.py's default) - the "min next round" value changes live as you
spend credits, and is only accurate right before you close the menu.
Capturing throughout the buy phase, combined with the Mac Mini's
minimum-value consensus (see credit_ocr.py - it takes the SMALLEST
validated reading, since credits only ever go down as you spend), means
the stored value naturally converges on your true final balance without
needing to remember to keep the menu open.

Real-world fix #3: a genuinely NEW buy phase (not just re-opening the
same one) tells the Mac Mini to clear its reading history via
reset_history() - without this, the previous round's readings were still
sitting in the consensus window, contaminating the new round's
consensus. Consuming BurstTimer's fresh-start flag is what correctly
distinguishes "new round" from "same round, re-opened."

Real-world fix #4: rather than always waiting out the full burst
duration, a run of consecutive "no number found" responses is treated as
a strong signal you've already closed the menu, ending the burst early
instead of continuing to capture pointless gameplay content for the
remainder of the window. The threshold is 10 - deliberately expressed in
readings but chosen as a DURATION: 2.5 seconds at the real 4-images/
second rate. It was 16 back when the effective rate was closer to 2/sec
(so ~8 seconds, long enough that it rarely fired within a 6-second
burst at all); once fix #5 below made 4/sec real, holding 16 would have
meant waiting 4 seconds to notice a closed menu. Retuned to keep the
wall-clock delay roughly where it should be rather than letting a
capture-rate change quietly redefine it.

Requires calibrate.py to have been run at least once first, and
agent_secret in agent_config.json to match the Mac Mini's own
ocr_agent_secret in config.json exactly - they're the same value on
both sides, checked directly against each other.

Requires the `keyboard` package for the global hotkey (works even while
Valorant has focus/is fullscreen) - not in requirements.txt's minimal
Mac Mini equivalent, since this only runs on the gaming PC.
"""
import io
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import keyboard
import requests
from PIL import ImageGrab

from burst_timer import BurstTimer
from region import load_config, is_calibrated, crop_to_region

CAPTURE_INTERVAL_WITHIN_BURST = 0.25  # 4 images/second
IDLE_CHECK_INTERVAL = 0.5  # how often to check whether a burst just started, while idle
# 2.5 seconds at the 4/sec rate above - see fix #4. Keep these two
# constants in step: this is a duration expressed in readings, so changing
# CAPTURE_INTERVAL_WITHIN_BURST without changing this silently changes how
# long the agent waits before deciding the buy menu closed.
CONSECUTIVE_FAILURES_BEFORE_EARLY_EXIT = 10

# Real-world fix #5: the 0.25s interval above was already "4 images/second"
# on paper, but the loop used to call capture_and_send() - screenshot AND
# the full network round trip to the Mac Mini AND its Tesseract OCR time -
# directly inline, so the NEXT tick couldn't start until the PREVIOUS
# request finished. If a round trip ever took longer than 0.25s (very
# plausible against a 2012 Mac Mini running OCR), the real observed rate
# quietly dropped well below 4/sec even though the constant said 4/sec.
# Fixed by decoupling: the screenshot itself still happens on the main
# loop's own precise 0.25s tick (see main() below), but the slow part -
# encode + POST + status handling - is handed off to a small bounded
# thread pool so a slow backend response never delays the next screenshot.
MAX_CONCURRENT_REQUESTS = 4  # in-flight requests before new ones queue behind them


class ConsecutiveFailureTracker:
    """
    Tracks consecutive "no number found" responses, separate from the
    main loop so this specific piece of logic is directly testable -
    real success/failure sequences, not just eyeballing the loop.
    """

    def __init__(self, threshold: int):
        self.threshold = threshold
        self._count = 0

    def record_status(self, status_code: "int | None") -> None:
        if status_code == 422:
            self._count += 1
        elif status_code == 200:
            self._count = 0
        # Any other status (401/503/network error/None) leaves the count
        # untouched - those are different problems entirely, unrelated to
        # whether the menu is still open.

    def should_early_exit(self) -> bool:
        return self._count >= self.threshold

    def reset(self) -> None:
        self._count = 0


def capture_and_send(config: dict, screenshot=None) -> "int | None":
    """
    Returns the response's HTTP status code, or None if the request itself
    failed (network error). Accepts an already-captured screenshot (see
    fix #5 above) so the grab itself always happens on the main loop's own
    precise timing tick rather than whenever a worker thread happens to
    get scheduled - callers that don't care about that precision (the
    existing tests included) can still omit it and let this grab fresh,
    same as before.
    """
    if screenshot is None:
        screenshot = ImageGrab.grab()
    cropped = crop_to_region(screenshot, config["region"])

    buffer = io.BytesIO()
    cropped.save(buffer, format="PNG")

    try:
        response = requests.post(
            config["backend_url"],
            data=buffer.getvalue(),
            headers={"X-Agent-Secret": config["agent_secret"]},
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        print(f"Could not reach the backend: {e}")
        return None

    if response.status_code == 200:
        body = response.json()
        print(f"OK - this reading: {body.get('credits')} | current consensus: {body.get('consensus')}")
    elif response.status_code == 401:
        print("401 - agent_secret doesn't match the Mac Mini's ocr_agent_secret. Check both configs match exactly.")
    elif response.status_code == 422:
        print("422 - no number found in the captured region.")
    elif response.status_code == 503:
        print("503 - the Mac Mini reports tesseract itself isn't available. This is a Mac Mini setup problem, not "
              "something wrong on this end.")
    else:
        print(f"Unexpected response: {response.status_code} - {response.text}")

    return response.status_code


def reset_history(config: dict) -> None:
    """Called once at the start of a genuinely new buy phase - clears the Mac Mini's reading history."""
    reset_url = config["backend_url"].rsplit("/", 1)[0] + "/reset"
    try:
        requests.post(reset_url, headers={"X-Agent-Secret": config["agent_secret"]}, timeout=10)
        print("New buy phase - reset the Mac Mini's reading history.")
    except requests.exceptions.RequestException as e:
        print(f"Could not reach the backend to reset history: {e}")


def main():
    config = load_config()

    if not is_calibrated(config):
        print("No region calibrated yet - run calibrate.py first.")
        sys.exit(1)

    if not config.get("agent_secret"):
        print("agent_secret is empty in agent_config.json - set it to match the Mac Mini's ocr_agent_secret.")
        sys.exit(1)

    # .get() with a fallback, not direct access - an existing
    # agent_config.json from before this field was renamed (originally
    # capture_interval_seconds, used for a different, now-removed timer
    # design) would otherwise crash here with a KeyError.
    burst_duration = config.get("burst_duration_seconds", 20)
    burst = BurstTimer(duration_seconds=burst_duration)
    keyboard.on_press_key("b", lambda _: burst.trigger())

    print(f"Watching for 'B' (buy menu) - capturing for up to {burst_duration}s each time it's pressed.")
    print(f"Sending to {config['backend_url']}")
    print("Press Ctrl+C to stop.")

    last_capture_at = 0.0
    failure_tracker = ConsecutiveFailureTracker(threshold=CONSECUTIVE_FAILURES_BEFORE_EARLY_EXIT)
    # Guards failure_tracker + burst.force_end() below, since those now get
    # touched from whichever worker thread happens to finish a request, not
    # just the main loop thread (fix #5).
    tracker_lock = threading.Lock()
    executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS, thread_name_prefix="ocr-send")

    def send_in_background(cfg: dict, screenshot) -> None:
        status = capture_and_send(cfg, screenshot=screenshot)
        with tracker_lock:
            failure_tracker.record_status(status)
            if failure_tracker.should_early_exit():
                print(f"{CONSECUTIVE_FAILURES_BEFORE_EARLY_EXIT} consecutive 'no number found' responses - "
                      f"assuming the buy menu already closed, ending this burst early.")
                burst.force_end()
                failure_tracker.reset()

    try:
        while True:
            if burst.is_active():
                if burst.consume_fresh_start():
                    # A genuinely NEW buy phase, not just re-opening the
                    # same one - the real fix for cross-round contamination.
                    reset_history(config)
                    with tracker_lock:
                        failure_tracker.reset()

                now = time.time()
                if now - last_capture_at >= CAPTURE_INTERVAL_WITHIN_BURST:
                    last_capture_at = now
                    # Grab happens right here, on-schedule, every tick -
                    # never delayed by a busy worker or a slow backend.
                    screenshot = ImageGrab.grab()
                    executor.submit(send_in_background, config, screenshot)

                time.sleep(0.05)
            else:
                time.sleep(IDLE_CHECK_INTERVAL)
    except KeyboardInterrupt:
        print("Stopping...")
        executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
