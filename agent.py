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

Real-world fix #3: starting a burst tells the Mac Mini to clear its
reading history via reset_history() - without this, the previous
round's readings were still sitting in the consensus window,
contaminating the new round's consensus. Consuming BurstTimer's
fresh-start flag is what makes that fire once per press rather than
once per capture tick. (This originally fired only for a burst that
started from idle, on the theory that re-opening the same menu should
keep its earlier readings; fix #6 below reverses that.)

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

Real-world fix #6, from a real log: after "10 consecutive 'no number
found' responses - assuming the buy menu already closed, ending this
burst early", nine MORE 422s kept printing. The early exit stops the
main loop capturing, but it does nothing about screenshots already
sitting in the thread pool's queue - that queue is unbounded, so
whenever the Mac Mini's round trip is slower than the 0.25s tick a
backlog builds up, and every item in it still gets POSTed after the
burst is over. Log noise is the mild half; the real problem is that one
of those late frames can OCR to a genuine number and land in the
consensus window after the menu closed.

Each capture is now stamped with BurstTimer's generation, and the worker
drops anything whose burst has ended or been superseded before it makes
the network call. The same mechanism handles the other half of the
report: pressing B again now cancels the burst in progress and starts a
clean one (see burst_timer.py's own fix #6 for why that reverses the
earlier "a re-press is an extension" rule), which both resets the Mac
Mini's history and invalidates the previous generation's queued work.

Real-world fix #7: the Mac Mini could not tell whether this agent was
running. Captures only travel during a burst - a couple of seconds per
round, nothing between them - so their absence is the normal state for
most of a match and says nothing about whether the process is alive. A
separate heartbeat now goes out on its own timer, which is what the admin
dashboard's OCR row reports, so "is the gaming PC ready" is answerable
before a stream rather than after the first !roulette comes back with the
full roster because no credits were ever read.

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

# Three of these fit inside the Mac Mini's own 45-second staleness window
# (ocr_agent.HEARTBEAT_TIMEOUT_SECONDS), so a couple of dropped pings in a
# row never make the dashboard claim this agent died. Those two constants
# belong together: shortening this one without widening that one there
# does nothing, lengthening it past a third of that window makes the
# dashboard flap.
HEARTBEAT_INTERVAL_SECONDS = 15


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


def capture_and_send(config: dict, cropped=None) -> "int | None":
    """
    Returns the response's HTTP status code, or None if the request itself
    failed (network error). Accepts an already-cropped image (see fix #5
    above) so the grab happens on the main loop's own precise timing tick
    rather than whenever a worker thread gets scheduled - callers that
    don't care about that precision (the existing tests included) can omit
    it and let this grab fresh, same as before.

    The CROP, not the full screenshot, is what gets handed over. A queued
    full-screen grab is around 6 MB of RGB at 1080p, so a backlog of a
    dozen is most of a gigabyte held in memory for no reason; the crop is
    a few hundred pixels. Cropping costs the main loop almost nothing next
    to the grab it already does.
    """
    if cropped is None:
        cropped = crop_to_region(ImageGrab.grab(), config["region"])

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


def endpoint(config: dict, name: str) -> str:
    """
    A sibling of the configured backend_url. Only backend_url is
    configured, so every other route is derived from it - one address to
    get right in agent_config.json rather than three that can disagree.
    """
    return config["backend_url"].rsplit("/", 1)[0] + "/" + name


def reset_history(config: dict) -> None:
    """Called once at the start of a genuinely new buy phase - clears the Mac Mini's reading history."""
    try:
        requests.post(endpoint(config, "reset"), headers={"X-Agent-Secret": config["agent_secret"]}, timeout=10)
        print("New buy phase - reset the Mac Mini's reading history.")
    except requests.exceptions.RequestException as e:
        print(f"Could not reach the backend to reset history: {e}")


def send_heartbeat(config: dict) -> "int | None":
    """
    One liveness ping. Returns the status code, or None if the request
    itself failed, so the caller can report a change of state.
    """
    try:
        response = requests.post(
            endpoint(config, "heartbeat"),
            headers={"X-Agent-Secret": config["agent_secret"]},
            timeout=10,
        )
    except requests.exceptions.RequestException:
        return None
    return response.status_code


def heartbeat_loop(config: dict, stop_event, sender=send_heartbeat, sleep=time.sleep) -> None:
    """
    Pings until told to stop, printing only when the answer CHANGES.

    That last part is the whole design of this function. At one ping every
    15 seconds a line per attempt would be a few thousand lines across a
    stream, burying the capture output that is actually worth reading -
    while a silent loop would hide the one event that matters, which is the
    link going down or coming back. So transitions print and steady states
    do not.

    The sender and sleep are injected so the transition logic can be tested
    against a scripted sequence of responses rather than a real Mac Mini
    and a real clock.
    """
    last_status = "unset"
    while not stop_event.is_set():
        status = sender(config)
        if status != last_status:
            if status == 200:
                print("Heartbeat: the Mac Mini can see this agent.")
            elif status == 401:
                print("Heartbeat: 401 - agent_secret doesn't match the Mac Mini's ocr_agent_secret.")
            elif status == 404:
                print("Heartbeat: 404 - this Mac Mini backend predates the heartbeat route. Pull and restart it.")
            elif status is None:
                print("Heartbeat: can't reach the Mac Mini. Captures won't get through either.")
            else:
                print(f"Heartbeat: unexpected response {status}.")
            last_status = status
        sleep(HEARTBEAT_INTERVAL_SECONDS)


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

    # Only a genuine down-transition counts as a press. Holding B makes the
    # OS emit a repeating stream of key-down events, and since every trigger
    # now cancels the burst in progress and resets the Mac Mini's history
    # (fix #6), letting auto-repeat through would restart the burst dozens of
    # times a second and never accumulate a single reading. Tracking the
    # release is exact, where a timing-based debounce would only be a guess.
    b_is_down = False

    def on_b_down(_):
        nonlocal b_is_down
        if b_is_down:
            return
        b_is_down = True
        burst.trigger()

    def on_b_up(_):
        nonlocal b_is_down
        b_is_down = False

    keyboard.on_press_key("b", on_b_down)
    keyboard.on_release_key("b", on_b_up)

    # Daemon thread: this only reports status, so a Ctrl+C should never
    # wait on it. The event is still set on the way out so a stop during
    # the sleep is clean rather than abandoned mid-request.
    heartbeat_stop = threading.Event()
    threading.Thread(
        target=heartbeat_loop,
        args=(config, heartbeat_stop),
        name="heartbeat",
        daemon=True,
    ).start()

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

    def send_in_background(cfg: dict, cropped, generation: int) -> None:
        # Checked HERE, in the worker, not at submit time - the whole point
        # is that this work may have sat in the pool's unbounded queue while
        # the burst it belongs to ended or was cancelled by a new B press.
        # Sending it anyway is not just log noise: a frame captured after
        # the menu closed can still OCR to a real number and land in the Mac
        # Mini's consensus window.
        if not burst.is_current(generation):
            return
        status = capture_and_send(cfg, cropped=cropped)
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
                    cropped = crop_to_region(ImageGrab.grab(), config["region"])
                    executor.submit(send_in_background, config, cropped, burst.generation)

                time.sleep(0.05)
            else:
                time.sleep(IDLE_CHECK_INTERVAL)
    except KeyboardInterrupt:
        print("Stopping...")
        heartbeat_stop.set()
        executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
