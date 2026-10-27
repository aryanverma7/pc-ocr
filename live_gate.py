"""
Decides whether the uploader should hold off because the streamer is live.
OBS is asked first; only when OBS cannot answer is the backend asked, and when
neither answers the upload goes ahead (a dead OBS must not stall uploads).

All inputs are injected callables returning True / False / None (unknown), so
tests need neither OBS nor a network.
"""
import time

RESUME_AFTER_SECONDS = 300  # stay paused this long after live was last seen
RECHECK_SECONDS = 30  # ask at most this often; between asks reuse the answer


class LiveGate:
    def __init__(self, obs_streaming, backend_live, clock=time.monotonic):
        self._obs, self._backend, self._clock = obs_streaming, backend_live, clock
        self._asked_at = None
        self._live = False
        self._last_live = None

    def should_pause(self) -> bool:
        now = self._clock()
        if self._asked_at is None or now - self._asked_at >= RECHECK_SECONDS:
            answer = self._obs()
            if answer is None:
                answer = self._backend()
            self._live = answer is True
            self._asked_at = now
            if self._live:
                self._last_live = now
        if self._live:
            return True
        return self._last_live is not None and now - self._last_live < RESUME_AFTER_SECONDS


def obs_streaming_from(cfg: dict):
    def ask():
        try:
            import obsws_python  # Windows-side dependency; imported lazily

            client = obsws_python.ReqClient(
                host=cfg.get("obs_ws_host", "localhost"),
                port=cfg.get("obs_ws_port", 4455),
                password=cfg.get("obs_ws_password", ""),
                timeout=3,
            )
        except Exception:
            return None
        try:
            return bool(client.get_stream_status().output_active)
        except Exception:
            return None
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    return ask


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
