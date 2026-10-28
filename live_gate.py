"""
Decides whether the uploader should hold off because the streamer is live.
The Mac Mini is asked (it asks Twitch); when it cannot answer the upload goes
ahead (a Mac Mini that is down must not stall uploads).

The input is an injected callable returning True / False / None (unknown), so
tests need no network.
"""
import time

RESUME_AFTER_SECONDS = 300  # stay paused this long after live was last seen
RECHECK_SECONDS = 30  # ask at most this often; between asks reuse the answer


class LiveGate:
    def __init__(self, is_live, clock=time.monotonic):
        self._is_live, self._clock = is_live, clock
        self._asked_at = None
        self._live = False
        self._last_live = None

    def should_pause(self) -> bool:
        now = self._clock()
        if self._asked_at is None or now - self._asked_at >= RECHECK_SECONDS:
            self._live = self._is_live() is True
            self._asked_at = now
            if self._live:
                self._last_live = now
        if self._live:
            return True
        return self._last_live is not None and now - self._last_live < RESUME_AFTER_SECONDS


def backend_live_from(base_url: str, secret: str, http):
    def ask():
        try:
            resp = http.get(
                f"{base_url.rstrip('/')}/api/uploader/live",
                headers={"X-Agent-Secret": secret},
                timeout=5,
            )
            if resp.status_code != 200:
                return None
            live = resp.json().get("live")
            return live if isinstance(live, bool) else None
        except Exception:
            return None

    return ask
