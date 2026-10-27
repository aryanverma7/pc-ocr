import sys
import types

import live_gate
from live_gate import LiveGate, backend_live_from, obs_streaming_from


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Answers:
    """Callable returning a settable answer, counting how often it is asked."""

    def __init__(self, value):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


def make(obs=None, backend=None):
    clock = Clock()
    obs, backend = Answers(obs), Answers(backend)
    return LiveGate(obs, backend, clock), obs, backend, clock


def test_not_live_does_not_pause():
    gate, *_ = make(obs=False)
    assert gate.should_pause() is False


def test_obs_streaming_pauses():
    gate, *_ = make(obs=True)
    assert gate.should_pause() is True


def test_resumes_only_300s_after_stream_ends():
    gate, obs, _, clock = make(obs=True)
    assert gate.should_pause()
    obs.value = False
    clock.t = 30
    assert gate.should_pause()  # first offline answer, still inside the grace
    clock.t = 299
    assert gate.should_pause()
    clock.t = 301
    assert not gate.should_pause()


def test_obs_unreachable_falls_back_to_backend():
    gate, _, backend, _ = make(obs=None, backend=True)
    assert gate.should_pause() is True
    assert backend.calls == 1


def test_neither_answers_means_upload():
    gate, *_ = make(obs=None, backend=None)
    assert gate.should_pause() is False


def test_backend_not_asked_when_obs_answers():
    gate, _, backend, _ = make(obs=False, backend=True)
    assert gate.should_pause() is False
    assert backend.calls == 0


def test_checks_at_most_every_30s():
    gate, obs, _, clock = make(obs=False)
    gate.should_pause()
    clock.t = 29
    gate.should_pause()
    assert obs.calls == 1
    clock.t = 30
    gate.should_pause()
    assert obs.calls == 2


def test_obs_streaming_from_returns_none_on_error(monkeypatch):
    def boom(**kw):
        raise ConnectionRefusedError

    monkeypatch.setitem(sys.modules, "obsws_python", types.SimpleNamespace(ReqClient=boom))
    assert obs_streaming_from({})() is None


def test_obs_streaming_from_reads_output_active_and_disconnects(monkeypatch):
    seen = {}

    class Client:
        def __init__(self, **kw):
            seen.update(kw)

        def get_stream_status(self):
            return types.SimpleNamespace(output_active=True)

        def disconnect(self):
            seen["closed"] = True

    monkeypatch.setitem(sys.modules, "obsws_python", types.SimpleNamespace(ReqClient=Client))
    assert obs_streaming_from({"obs_ws_password": "pw"})() is True
    assert seen == {"host": "localhost", "port": 4455, "password": "pw", "timeout": 3, "closed": True}


class Resp:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status_code, self.body, self.bad_json = status, body, bad_json

    def json(self):
        if self.bad_json:
            raise ValueError
        return self.body


class Http:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.args = resp, exc, None

    def get(self, url, **kw):
        self.args = (url, kw)
        if self.exc:
            raise self.exc
        return self.resp


def test_backend_live_from_passes_real_bools():
    for v in (True, False):
        http = Http(Resp(body={"live": v}))
        assert backend_live_from("http://m/", "s", http)() is v
    url, kw = http.args
    assert url == "http://m/api/uploader/live"
    assert kw == {"headers": {"X-Agent-Secret": "s"}, "timeout": 5}


def test_backend_live_from_unknown_cases_are_none():
    for http in (
        Http(Resp(body={"live": None})),
        Http(Resp(body={"live": 1})),
        Http(Resp(body={})),
        Http(Resp(status=401, body={"live": True})),
        Http(Resp(bad_json=True)),
        Http(exc=OSError()),
    ):
        assert backend_live_from("http://m", "s", http)() is None
