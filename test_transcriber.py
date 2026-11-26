import json
import logging
import subprocess
from pathlib import Path

import pytest

import transcriber
from uploader import Refused

JOB = {"iteration_id": 7, "token": "tok", "file_id": "F1", "access_token": "SECRET-ACCESS", "name": "v1", "size": 3}
SEGS = [{"start": 0.0, "end": 1.5, "text": "ye clutch dekho"}]


class FakeResp:
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        yield b"abc"


class FakeHttp:
    def __init__(self):
        self.gets = []

    def get(self, url, **kw):
        self.gets.append((url, kw))
        return FakeResp()


class FakeBackend:
    def __init__(self, complete_error=None):
        self.calls = []
        self.complete_error = complete_error

    def complete(self, iid, token, segments, model):
        self.calls.append(("complete", iid, token, segments, model))
        if self.complete_error:
            raise self.complete_error
        return {"ok": True}

    def release(self, iid, token, error=None):
        self.calls.append(("release", iid, token, error))
        return {"ok": True}


class FakeProc:
    """Exits with `code` after `waits` timed-out waits, writing the worker's JSON first."""

    def __init__(self, args, code, waits):
        self.out, self.code, self.waits, self.killed, self.done = Path(args[3]), code, waits, False, False

    def poll(self):
        return (-9 if self.killed else self.code) if self.done or self.killed else None

    def wait(self, timeout=None):
        if self.killed:
            return -9
        if self.waits > 0:
            self.waits -= 1
            raise subprocess.TimeoutExpired("worker", timeout)
        if self.code == 0:
            self.out.write_text(json.dumps({"segments": SEGS, "model": "hinglish-apex-ct2"}))
        self.done = True
        return self.code

    def kill(self):
        self.killed = True


def _spawn(code=0, waits=0, record=None):
    def spawn(args, **kw):
        p = FakeProc(args, code, waits)
        if record is not None:
            record.append(p)
        return p
    return spawn


def _clock():
    t = [0.0]

    def clock():
        t[0] += 10  # every look at the clock is past the abort-check interval
        return t[0]
    return clock


def test_job_downloads_runs_worker_posts_and_cleans_up(tmp_path, caplog):
    caplog.set_level(logging.INFO, "transcriber")
    http, backend = FakeHttp(), FakeBackend()
    r = transcriber.run_job(JOB, backend, http, lambda: False, spawn=_spawn(), workdir=tmp_path,
                            clock=_clock())
    assert r == "done"
    url, kw = http.gets[0]
    assert "F1" in url and "SECRET-ACCESS" not in url
    assert kw["headers"]["Authorization"] == "Bearer SECRET-ACCESS"
    assert backend.calls == [("complete", 7, "tok", SEGS, "hinglish-apex-ct2")]
    assert list(tmp_path.iterdir()) == []
    assert "SECRET-ACCESS" not in caplog.text


def test_abort_mid_run_kills_releases_and_cleans_up(tmp_path):
    procs, backend = [], FakeBackend()
    asks = iter([False, False, True])  # download chunk, first wait, second wait
    r = transcriber.run_job(JOB, backend, FakeHttp(), lambda: next(asks),
                            spawn=_spawn(waits=5, record=procs), workdir=tmp_path, clock=_clock())
    assert r == "released"
    assert procs[0].killed
    assert backend.calls == [("release", 7, "tok", None)]
    assert list(tmp_path.iterdir()) == []


def test_worker_crash_releases_with_error(tmp_path):
    backend = FakeBackend()
    r = transcriber.run_job(JOB, backend, FakeHttp(), lambda: False, spawn=_spawn(code=1),
                            workdir=tmp_path, clock=_clock())
    assert r == "failed"
    kind, iid, token, error = backend.calls[0]
    assert (kind, iid, token) == ("release", 7, "tok") and "exit code 1" in error
    assert list(tmp_path.iterdir()) == []


def test_409_on_complete_is_dropped(tmp_path):
    backend = FakeBackend(complete_error=Refused("stale", 409))
    r = transcriber.run_job(JOB, backend, FakeHttp(), lambda: False, spawn=_spawn(),
                            workdir=tmp_path, clock=_clock())
    assert r == "released"
    assert [c[0] for c in backend.calls] == ["complete"]  # no release: the new claim owns it


class Gate:
    def __init__(self, ready):
        self._ready, self.observed = ready, 0

    def observe(self):
        self.observed += 1

    def ready(self):
        return self._ready


class LoopBackend:
    def __init__(self):
        self.nexts = 0

    def next(self):
        self.nexts += 1
        return {"enabled": True, "live": False, "job": dict(JOB)}


def _stop_after(n):
    c = [0]

    def stop():
        c[0] += 1
        return c[0] > n
    return stop


def test_loop_never_asks_for_a_job_while_not_ready():
    gate, backend = Gate(False), LoopBackend()
    transcriber.loop(gate, backend, run=lambda job: pytest.fail("ran"), stop=_stop_after(20),
                     sleep=lambda s: None, clock=_clock())
    assert gate.observed == 20 and backend.nexts == 0


def test_loop_runs_job_when_ready():
    gate, backend, ran = Gate(True), LoopBackend(), []
    t = [0.0]

    def sleep(s):
        t[0] += s
    transcriber.loop(gate, backend, run=ran.append, stop=_stop_after(12), sleep=sleep,
                     clock=lambda: t[0])
    assert backend.nexts == 2  # t=0 and t=60, never more often than POLL_SECONDS
    assert ran[0]["iteration_id"] == 7


def test_status_check_stops_on_paused_or_live_and_not_on_failure():
    answers = iter([{"enabled": True, "live": False}, {"enabled": False, "live": False}])

    class B:
        def status(self):
            return next(answers)
    t = [0.0]
    check = transcriber.status_says_stop(B(), clock=lambda: t[0])
    assert check() is False
    t[0] = 10
    assert check() is False  # cached: not asked again within STATUS_SECONDS
    t[0] = 30
    assert check() is True

    class Down:
        def status(self):
            raise OSError("down")
    assert transcriber.status_says_stop(Down(), clock=lambda: 0.0)() is False


def test_block_list_needs_a_list_of_names():
    assert transcriber.block_list({}) == list(transcriber.DEFAULT_GAMES)
    assert transcriber.block_list({"transcriber_block_processes": ["cs2.exe"]}) == ["cs2.exe"]
    for bad in ("cs2.exe", [], [""], [3], {"a": 1}):
        assert transcriber.block_list({"transcriber_block_processes": bad}) == list(transcriber.DEFAULT_GAMES)


def test_malformed_iteration_id_never_becomes_a_path(tmp_path):
    for bad in ("..\\..\\x", "../x", "1/2", None, -3, "7 ; x"):
        assert transcriber.run_job({"iteration_id": bad, "token": "t"}, None, None, lambda: False,
                                   workdir=tmp_path / "w") == "failed"
    assert not (tmp_path / "w").exists()


def test_an_error_in_the_abort_check_still_kills_the_worker(tmp_path):
    procs, backend = [], FakeBackend()
    asks = iter([False])  # the download's check passes; the first check while transcribing raises

    def should_abort():
        try:
            return next(asks)
        except StopIteration:
            raise RuntimeError("status check blew up") from None
    with pytest.raises(RuntimeError):
        transcriber.run_job(JOB, backend, FakeHttp(), should_abort, spawn=_spawn(waits=5, record=procs),
                            workdir=tmp_path, clock=_clock())
    assert procs[0].killed
    assert list(tmp_path.iterdir()) == []
