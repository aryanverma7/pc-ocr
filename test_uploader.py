import datetime
import json
import os
import sys

import pytest
import requests

import uploader
from drive_upload import SessionExpired
from live_gate import RECHECK_SECONDS

MiB = 1024 * 1024


def make_file(tmp_path, name="vod.mp4", size=100):
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return p


def queued(jobs_dir):
    return sorted(p.name for p in jobs_dir.glob("*.json"))


def load_one(jobs_dir):
    [p] = list(jobs_dir.glob("*.json"))
    return json.loads(p.read_text())


class FakeBackend:
    """Idempotent by job_id, like the real route."""
    def __init__(self, status="uploading"):
        self.status = status
        self.projects = {}
        self.registers = []
        self.completes = []
        self.complete_errors = []
        self.sessions = 0

    def register(self, job, new_session=False):
        self.registers.append((job["job_id"], new_session))
        pid = self.projects.setdefault(job["job_id"], f"p{len(self.projects) + 1}")
        if new_session or not hasattr(self, "_url"):
            self.sessions += 1
            self._url = f"https://upload/{self.sessions}"
        return {"project_id": pid, "kind": "vod", "status": self.status,
                "upload_url": "" if self.status == "ready" else self._url}

    def complete(self, project_id, file_id):
        if self.complete_errors:
            raise self.complete_errors.pop(0)
        self.completes.append((project_id, file_id))
        return {"status": "ready"}


class NoBackend:
    def register(self, *a, **k):
        raise AssertionError("backend must not be called")

    complete = register


class Gate:
    def __init__(self, pauses=0):
        self.pauses = pauses

    def should_pause(self):
        if self.pauses:
            self.pauses -= 1
            return True
        return False


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append((a, k))


@pytest.fixture
def drive(monkeypatch):
    """Fake query_offset/send_from; records what the uploader asked for."""
    state = {"offset": 0, "sends": [], "queries": [], "expired": set()}

    def query_offset(http, url, total):
        state["queries"].append(url)
        if url in state["expired"]:
            raise SessionExpired("gone")
        return state["offset"]

    def send_from(http, url, path, total, offset, on_chunk):
        state["sends"].append((url, offset))
        if not on_chunk(min(offset + 8 * MiB, total)):
            return {}
        return {"id": "file-1"}

    monkeypatch.setattr(uploader, "query_offset", query_offset)
    monkeypatch.setattr(uploader, "send_from", send_from)
    return state


def enqueue_one(tmp_path, size=100, duration=lambda p: 1234.0):
    f = make_file(tmp_path, size=size)
    jobs = tmp_path / "uploads"
    assert uploader.enqueue([str(f)], jobs_dir=jobs, duration=duration) == []
    return f, jobs, load_one(jobs)


def run(job, backend, jobs, gate=None, sleep=None, stop=lambda: False, progress=None):
    return uploader.run_job(job, backend, None, gate or Gate(), progress or Recorder(),
                            sleep=sleep or Recorder(), stop=stop, jobs_dir=jobs)


# --- enqueue -----------------------------------------------------------------

def test_enqueue_writes_job_and_skips_duplicates(tmp_path):
    f, jobs, job = enqueue_one(tmp_path)
    assert job["path"] == os.path.abspath(f)
    assert job["size"] == 100 and job["mtime"] == os.stat(f).st_mtime
    assert job["mime"] == "video/mp4"
    assert queued(jobs) == [f"{job['job_id']}.json"]
    msgs = uploader.enqueue([str(f)], jobs_dir=jobs, duration=lambda p: None)
    assert msgs == ["vod.mp4 is already queued"]
    assert len(queued(jobs)) == 1


def test_enqueue_refuses_missing_and_empty(tmp_path):
    empty = make_file(tmp_path, "empty.mkv", size=0)
    jobs = tmp_path / "uploads"
    msgs = uploader.enqueue([str(tmp_path / "gone.mp4"), str(empty)], jobs_dir=jobs,
                            duration=lambda p: None)
    assert msgs == ["gone.mp4 was not found", "empty.mkv is empty"]
    assert queued(jobs) == []


def test_enqueue_records_duration_and_local_date(tmp_path):
    f = make_file(tmp_path, "clip.weird")
    ts = datetime.datetime(2026, 10, 26, 23, 30).timestamp()  # local time
    os.utime(f, (ts, ts))
    jobs = tmp_path / "uploads"
    seen = []
    uploader.enqueue([str(f)], jobs_dir=jobs, duration=lambda p: seen.append(p) or 42.5)
    job = load_one(jobs)
    assert seen == [os.path.abspath(f)]
    assert job["duration_s"] == 42.5
    assert job["stream_date"] == "2026-10-26"
    assert job["mime"] == "video/mp4"  # unknown extension falls back


def test_media_duration_failure_is_none(tmp_path):
    assert uploader.media_duration(str(tmp_path / "nope.mp4")) is None


# --- run_job -----------------------------------------------------------------

def test_run_job_happy_path_completes_and_deletes_job_file(tmp_path, drive):
    f, jobs, job = enqueue_one(tmp_path)
    backend, progress = FakeBackend(), Recorder()
    assert run(job, backend, jobs, progress=progress) == "done"
    assert backend.registers == [(job["job_id"], False)]
    assert backend.completes == [("p1", "file-1")]
    assert drive["sends"] == [("https://upload/1", 0)]
    assert ((("vod.mp4", 100, 100), {}) in progress.calls)
    assert queued(jobs) == []


def test_run_job_resumes_from_server_offset(tmp_path, drive):
    f, jobs, job = enqueue_one(tmp_path, size=20 * MiB)
    job.update(project_id="p9", upload_url="https://upload/old")
    drive["offset"] = 8 * MiB
    backend = FakeBackend()
    assert run(job, backend, jobs) == "done"
    assert drive["sends"] == [("https://upload/old", 8 * MiB)]
    assert backend.registers == []
    assert backend.completes == [("p9", "file-1")]


def test_run_job_restarts_on_session_expired(tmp_path, drive, monkeypatch):
    f, jobs, job = enqueue_one(tmp_path)
    job.update(project_id="p1", upload_url="https://upload/old")
    drive["expired"].add("https://upload/old")  # the fake answers 0 for the new one
    backend = FakeBackend()
    saved = []
    real_save = uploader._save
    monkeypatch.setattr(uploader, "_save", lambda j, d: (saved.append(dict(j)), real_save(j, d)))
    drive_q = drive["queries"]
    assert run(job, backend, jobs) == "done"
    assert backend.registers == [(job["job_id"], True)]
    assert saved[-1]["upload_url"] == "https://upload/1"
    assert drive_q == ["https://upload/old", "https://upload/1"]
    assert drive["sends"] == [("https://upload/1", 0)]


def test_run_job_drops_when_file_changed(tmp_path, drive):
    f, jobs, job = enqueue_one(tmp_path)
    os.utime(f, (job["mtime"] + 10, job["mtime"] + 10))
    assert run(job, NoBackend(), jobs) == \
        "dropped: vod.mp4 changed or moved since it was sent; send it again"
    assert queued(jobs) == []


def test_run_job_drops_when_file_missing(tmp_path, drive):
    f, jobs, job = enqueue_one(tmp_path)
    f.unlink()
    assert run(job, NoBackend(), jobs).startswith("dropped: vod.mp4 changed or moved")
    assert queued(jobs) == []


def test_run_job_reuses_project_after_crash_between_register_and_save(tmp_path, drive, monkeypatch):
    f, jobs, job = enqueue_one(tmp_path)
    backend = FakeBackend()
    real_save = uploader._save

    def crash(j, d):
        raise KeyboardInterrupt("power cut")

    monkeypatch.setattr(uploader, "_save", crash)
    with pytest.raises(KeyboardInterrupt):
        run(job, backend, jobs)
    monkeypatch.setattr(uploader, "_save", real_save)
    job = load_one(jobs)  # what the next boot reads: no project_id saved
    assert "project_id" not in job
    assert run(job, backend, jobs) == "done"
    assert [r[0] for r in backend.registers] == [job["job_id"]] * 2
    assert len(backend.projects) == 1


def test_run_job_retries_complete_until_backend_back(tmp_path, drive):
    f, jobs, job = enqueue_one(tmp_path)
    backend, sleep = FakeBackend(), Recorder()
    backend.complete_errors = [requests.ConnectionError("down"), requests.ConnectionError("down")]
    assert run(job, backend, jobs, sleep=sleep) == "done"
    assert [c[0][0] for c in sleep.calls] == [5, 10]
    assert backend.completes == [("p1", "file-1")]


def test_run_job_drops_on_refused(tmp_path, drive):
    f, jobs, job = enqueue_one(tmp_path)
    http = FakeHttp(resp(507, {"error": "Drive is too full for this file",
                               "needed": 12.3e9, "free": 4.1e9}))
    result = run(job, uploader.Backend("http://mini:8765", "s", http), jobs)
    assert result == "dropped: Drive is too full for vod.mp4: needs 12.3 GB, 4.1 GB free"
    assert queued(jobs) == []


def test_run_job_bad_secret_propagates_and_keeps_job(tmp_path, drive):
    f, jobs, job = enqueue_one(tmp_path)
    http = FakeHttp(resp(401, {"error": "Invalid or missing agent secret"}))
    with pytest.raises(uploader.BadSecret) as e:
        run(job, uploader.Backend("http://mini:8765", "s", http), jobs)
    assert not isinstance(e.value, uploader.BackendTooOld)
    assert len(queued(jobs)) == 1


def test_run_job_other_401_means_backend_too_old_and_keeps_job(tmp_path, drive):
    f, jobs, job = enqueue_one(tmp_path)
    http = FakeHttp(resp(401, {"error": "Not authenticated - open /auth/login in a browser tab, then retry."}))
    with pytest.raises(uploader.BackendTooOld) as e:
        run(job, uploader.Backend("http://mini:8765", "s", http), jobs)
    assert e.value.message == "The Mac Mini backend needs updating before the uploader can work"
    assert len(queued(jobs)) == 1


def test_run_job_drops_when_file_changes_during_mid_file_pause(tmp_path, monkeypatch):
    f, jobs, job = enqueue_one(tmp_path, size=100)
    sends = []
    monkeypatch.setattr(uploader, "query_offset", lambda http, url, total: 0)

    def send_from(http, url, path, total, offset, on_chunk):
        while offset < total:
            sends.append(offset)
            offset += 50
            if not on_chunk(offset):
                return {}
        return {"id": "file-1"}

    monkeypatch.setattr(uploader, "send_from", send_from)

    class MidGate:  # free for the first chunk, then one pause
        answers = [False, True, False]

        def should_pause(self):
            return self.answers.pop(0) if self.answers else False

    def sleep(_):
        os.utime(f, (1, 1))  # the file is edited while uploads wait

    backend = FakeBackend()
    result = run(job, backend, jobs, gate=MidGate(), sleep=sleep)
    assert result.startswith("dropped: vod.mp4 changed or moved")
    assert sends == [0]  # nothing after the pause
    assert backend.completes == [] and queued(jobs) == []


def test_run_job_ready_project_skips_upload(tmp_path, drive):
    f, jobs, job = enqueue_one(tmp_path)
    backend = FakeBackend(status="ready")
    assert run(job, backend, jobs) == "done"
    assert drive["sends"] == [] and drive["queries"] == []
    assert backend.completes == []
    assert queued(jobs) == []


def test_run_job_waits_while_gate_paused(tmp_path, drive):
    f, jobs, job = enqueue_one(tmp_path)
    sleep, progress = Recorder(), Recorder()
    assert run(job, FakeBackend(), jobs, gate=Gate(pauses=2), sleep=sleep,
               progress=progress) == "done"
    assert [c[0][0] for c in sleep.calls] == [RECHECK_SECONDS] * 2
    assert sum(1 for c in progress.calls if c[1].get("paused")) == 2
    assert drive["sends"] == [("https://upload/1", 0)]  # paused before any bytes went


def test_run_job_stop_keeps_job(tmp_path, drive):
    f, jobs, job = enqueue_one(tmp_path)
    assert run(job, FakeBackend(), jobs, gate=Gate(pauses=5), stop=lambda: True) == "stopped"
    assert drive["sends"] == []
    assert len(queued(jobs)) == 1


def test_run_job_temporary_upload_error_resumes_and_resets_backoff(tmp_path, drive, monkeypatch):
    f, jobs, job = enqueue_one(tmp_path, size=30 * MiB)
    sends = []

    def send_from(http, url, path, total, offset, on_chunk):
        sends.append(offset)
        if offset < 16 * MiB:  # one chunk lands, then the line drops
            on_chunk(offset + 8 * MiB)
            drive["offset"] = offset + 8 * MiB
            raise requests.ConnectionError("x")
        return {"id": "file-1"}

    monkeypatch.setattr(uploader, "send_from", send_from)
    sleep = Recorder()
    assert run(job, FakeBackend(), jobs, sleep=sleep) == "done"
    assert sends == [0, 8 * MiB, 16 * MiB]
    assert [c[0][0] for c in sleep.calls] == [5, 5]  # progress resets the backoff


def test_run_job_upload_error_without_progress_backs_off(tmp_path, drive, monkeypatch):
    f, jobs, job = enqueue_one(tmp_path)
    errors = [requests.ConnectionError("x")] * 3

    def send_from(http, url, path, total, offset, on_chunk):
        if errors:
            raise errors.pop()
        return {"id": "file-1"}

    monkeypatch.setattr(uploader, "send_from", send_from)
    sleep = Recorder()
    assert run(job, FakeBackend(), jobs, sleep=sleep) == "done"
    assert [c[0][0] for c in sleep.calls] == [5, 10, 20]


# --- Backend -----------------------------------------------------------------

def resp(status, body):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode()
    return r


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        return self.responses.pop(0)


def test_backend_register_sends_job_fields_and_secret():
    http = FakeHttp(resp(200, {"project_id": "p1", "status": "uploading", "upload_url": "u"}))
    job = {"job_id": "j", "path": "/a/b/vod.mp4", "size": 5, "mime": "video/mp4",
           "duration_s": None, "stream_date": "2026-10-26", "mtime": 1.0}
    assert uploader.Backend("http://mini:8765/", "sek", http).register(job)["project_id"] == "p1"
    url, body, headers = http.calls[0]
    assert url == "http://mini:8765/api/uploader/projects"
    assert body == {"job_id": "j", "filename": "vod.mp4", "size": 5, "mime": "video/mp4",
                    "duration_s": None, "stream_date": "2026-10-26"}
    assert headers == {"X-Agent-Secret": "sek"}


def test_backend_complete_and_errors():
    http = FakeHttp(resp(200, {"status": "ready"}), resp(404, {}), resp(503, {"error": "x"}),
                    resp(409, {"error": "This VOD was deleted"}))
    b = uploader.Backend("http://mini:8765", "s", http)
    assert b.complete("p1", "f1") == {"status": "ready"}
    assert http.calls[0][:2] == ("http://mini:8765/api/uploader/projects/p1/complete",
                                 {"file_id": "f1"})
    with pytest.raises(uploader.Refused) as e:
        b.complete("p1", "f1")
    assert e.value.status == 404 and e.value.message
    with pytest.raises(requests.HTTPError):
        b.complete("p1", "f1")
    with pytest.raises(uploader.Refused, match="This VOD was deleted"):
        b.complete("p1", "f1")


# --- lock and drain ----------------------------------------------------------

def test_try_lock_is_exclusive(tmp_path):
    first = uploader.try_lock(tmp_path)
    assert first is not None
    assert uploader.try_lock(tmp_path) is None
    uploader.unlock(first)
    again = uploader.try_lock(tmp_path)
    assert again is not None
    uploader.unlock(again)


def test_drain_picks_up_job_added_while_unlocking(tmp_path):
    jobs = tmp_path / "uploads"
    first = make_file(tmp_path, "a.mp4")
    late = make_file(tmp_path, "b.mp4")
    uploader.enqueue([str(first)], jobs_dir=jobs, duration=lambda p: None)
    ran = []

    def fake_run(job, jobs_dir):
        ran.append(os.path.basename(job["path"]))
        (jobs_dir / f"{job['job_id']}.json").unlink()
        return "done"

    hooked = []

    def after_unlock():
        if not hooked:  # the sender that lost the lock race writes now
            hooked.append(1)
            uploader.enqueue([str(late)], jobs_dir=jobs, duration=lambda p: None)

    fh = uploader.try_lock(jobs)
    assert uploader.drain(jobs, fh, fake_run, after_unlock=after_unlock) == ["done", "done"]
    assert ran == ["a.mp4", "b.mp4"]
    held = uploader.try_lock(jobs)  # drain ended with the lock released
    assert held is not None
    uploader.unlock(held)


def test_drain_oldest_first_and_quarantines_bad_json(tmp_path):
    jobs = tmp_path / "uploads"
    for name in ("new.mp4", "old.mp4"):
        uploader.enqueue([str(make_file(tmp_path, name))], jobs_dir=jobs, duration=lambda p: None)
    for p in jobs.glob("*.json"):
        j = json.loads(p.read_text())
        j["created_at"] = 1.0 if j["path"].endswith("old.mp4") else 2.0
        p.write_text(json.dumps(j))
    (jobs / "broken.json").write_text("{not json")
    ran = []

    def fake_run(job, jobs_dir):
        ran.append(os.path.basename(job["path"]))
        (jobs_dir / f"{job['job_id']}.json").unlink()
        return "done"

    assert uploader.drain(jobs, uploader.try_lock(jobs), fake_run) == ["done", "done"]
    assert ran == ["old.mp4", "new.mp4"]
    assert (jobs / "broken.json.bad").exists()


def test_drain_stops_on_stopped(tmp_path):
    jobs = tmp_path / "uploads"
    uploader.enqueue([str(make_file(tmp_path))], jobs_dir=jobs, duration=lambda p: None)
    assert uploader.drain(jobs, uploader.try_lock(jobs), lambda job, jobs_dir: "stopped") \
        == ["stopped"]
    assert len(queued(jobs)) == 1


# --- review fixes ------------------------------------------------------------

def test_run_job_drops_when_file_changes_during_gate_pause(tmp_path, drive):
    f, jobs, job = enqueue_one(tmp_path)

    def sleep(seconds):  # the streamer trims the file while the stream runs
        os.utime(f, (job["mtime"] + 60, job["mtime"] + 60))

    result = run(job, FakeBackend(), jobs, gate=Gate(pauses=1), sleep=sleep)
    assert result == "dropped: vod.mp4 changed or moved since it was sent; send it again"
    assert drive["sends"] == []
    assert queued(jobs) == []


def test_run_job_rechecks_file_before_retrying(tmp_path, drive, monkeypatch):
    f, jobs, job = enqueue_one(tmp_path)
    sends = []

    def send_from(http, url, path, total, offset, on_chunk):
        sends.append(offset)
        f.unlink()
        raise requests.ConnectionError("x")

    monkeypatch.setattr(uploader, "send_from", send_from)
    assert run(job, FakeBackend(), jobs).startswith("dropped: vod.mp4 changed or moved")
    assert sends == [0]


def test_run_job_unexpected_error_drops_instead_of_stalling(tmp_path, drive, monkeypatch):
    f, jobs, job = enqueue_one(tmp_path)

    def send_from(*a):
        raise RuntimeError("unexpected status 418")

    monkeypatch.setattr(uploader, "send_from", send_from)
    assert run(job, FakeBackend(), jobs) == \
        "dropped: vod.mp4 could not be sent (RuntimeError); send it again"
    assert queued(jobs) == []


def test_pending_skips_unreadable_job_without_quarantining(tmp_path, monkeypatch):
    jobs = tmp_path / "uploads"
    uploader.enqueue([str(make_file(tmp_path))], jobs_dir=jobs, duration=lambda p: None)
    real = uploader.Path.read_text

    def locked(self, *a, **k):
        if self.suffix == ".json":
            raise PermissionError("antivirus")
        return real(self, *a, **k)

    monkeypatch.setattr(uploader.Path, "read_text", locked)
    assert uploader._pending(jobs) == []
    monkeypatch.setattr(uploader.Path, "read_text", real)
    assert len(queued(jobs)) == 1 and not list(jobs.glob("*.bad"))
    assert len(uploader._pending(jobs)) == 1


def test_pending_tolerates_bad_job_already_renamed(tmp_path, monkeypatch):
    jobs = tmp_path / "uploads"
    jobs.mkdir()
    (jobs / "broken.json").write_text("{not json")

    def gone(src, dst):
        raise FileNotFoundError(src)  # another process quarantined it first

    monkeypatch.setattr(uploader.os, "replace", gone)
    assert uploader._pending(jobs) == []


# --- main ---------------------------------------------------------------------

@pytest.fixture
def app(tmp_path, monkeypatch):
    jobs = tmp_path / "uploads"
    monkeypatch.setattr(uploader, "JOBS_DIR", jobs)
    monkeypatch.setattr(uploader, "_setup_logging", lambda: None)
    monkeypatch.setattr(uploader, "load_config", lambda: {
        "backend_url": "https://hub.example/api/ocr/credit-report", "agent_secret": "s"})
    monkeypatch.delitem(sys.modules, "pystray", raising=False)
    return jobs


def test_main_resume_with_empty_queue_exits_without_tray(app):
    assert uploader.main(["--resume"]) == 0
    assert "pystray" not in sys.modules


def test_main_exits_when_lock_held(app, tmp_path, monkeypatch):
    clip = make_file(tmp_path, "clip.mp4")
    monkeypatch.setattr(uploader, "media_duration", lambda p: 42.0)
    held = uploader.try_lock(app)
    try:
        assert uploader.main([str(clip)]) == 0
    finally:
        uploader.unlock(held)
    assert "pystray" not in sys.modules
    assert load_one(app)["path"] == str(clip)  # the running worker takes it
    [notice] = (app / "notices").glob("*.txt")  # and shows this summary
    assert notice.read_text(encoding="utf-8") == "clip.mp4: 0:42, clip"


def test_main_without_secret_returns_1(app, monkeypatch):
    monkeypatch.setattr(uploader, "load_config", lambda: {"backend_url": "x", "agent_secret": ""})
    assert uploader.main(["--resume"]) == 1


def test_enqueue_summary_line_formats_clip_and_vod():
    assert uploader.summary_line("C:\\v\\clip_42.mp4", 42.4) == "clip_42.mp4: 0:42, clip"
    assert uploader.summary_line("/v/stream.mkv", 3 * 3600 + 12 * 60 + 5) == "stream.mkv: 3:12:05, VOD"
    assert uploader.summary_line("/v/five.mp4", 300) == "five.mp4: 5:00, VOD"
    assert uploader.summary_line("/v/odd.mp4", None) == "odd.mp4: length unknown, VOD"


def test_base_url_strips_path():
    assert uploader.base_url("https://hub.example/api/ocr/credit-report") == "https://hub.example"
    assert uploader.base_url("http://10.0.0.5:8765/api/ocr/credit-report") == "http://10.0.0.5:8765"


def fake_pystray(notes):
    class Icon:
        def __init__(self, name, image, title, menu):
            self.title, self.stopped = title, False

        def notify(self, text, title=None):
            notes.append(text)

        def run(self, setup):
            setup(self)

        def stop(self):
            self.stopped = True

    return type(sys)("pystray"), Icon


def test_main_tray_shows_summary_and_drops_then_releases_lock(app, tmp_path, monkeypatch):
    notes = []
    mod, Icon = fake_pystray(notes)
    mod.Icon, mod.Menu, mod.MenuItem = Icon, lambda *a: a, lambda *a: a
    monkeypatch.setitem(sys.modules, "pystray", mod)
    monkeypatch.setattr(uploader, "_icon_image", lambda: None)
    monkeypatch.setattr(uploader, "NOTIFY_LINGER_SECONDS", 0)
    monkeypatch.setattr(uploader, "media_duration", lambda p: 42.0)
    monkeypatch.setattr(uploader, "run_job", lambda job, *a, jobs_dir, **k: (
        uploader._forget(job, jobs_dir), "dropped: clip.mp4 was refused")[1])
    clip = make_file(tmp_path, "clip.mp4")
    assert uploader.main([str(clip), str(tmp_path / "gone.mp4")]) == 0
    assert notes == ["clip.mp4: 0:42, clip\ngone.mp4 was not found", "clip.mp4 was refused"]
    held = uploader.try_lock(app)  # drain released it
    assert held is not None
    uploader.unlock(held)


def test_main_bad_secret_notifies(app, tmp_path, monkeypatch):
    notes = []
    mod, Icon = fake_pystray(notes)
    mod.Icon, mod.Menu, mod.MenuItem = Icon, lambda *a: a, lambda *a: a
    monkeypatch.setitem(sys.modules, "pystray", mod)
    monkeypatch.setattr(uploader, "_icon_image", lambda: None)
    monkeypatch.setattr(uploader, "NOTIFY_LINGER_SECONDS", 0)

    def bad(*a, **k):
        raise uploader.BadSecret("no", 401)

    monkeypatch.setattr(uploader, "run_job", bad)
    uploader.enqueue([str(make_file(tmp_path))], app, lambda p: None)
    assert uploader.main(["--resume"]) == 0
    assert notes == ["The agent secret is wrong - check agent_config.json"]
    assert len(queued(app)) == 1  # kept for after the secret is fixed


def tray(monkeypatch, notes, Icon=None):
    mod, FakeIcon = fake_pystray(notes)
    mod.Icon, mod.Menu, mod.MenuItem = Icon or FakeIcon, lambda *a: a, lambda *a: a
    monkeypatch.setitem(sys.modules, "pystray", mod)
    monkeypatch.setattr(uploader, "_icon_image", lambda: None)
    monkeypatch.setattr(uploader, "NOTIFY_LINGER_SECONDS", 0)
    return FakeIcon


def test_notices_left_by_losers_are_shown_and_deleted_by_the_worker(app, tmp_path, monkeypatch):
    uploader._post_notice(["a.mp4: 0:10, clip"], app)  # a loser, while nobody worked
    uploader._post_notice(["b.mp4 is empty"], app)
    notes = []
    tray(monkeypatch, notes)
    assert uploader.main(["--resume"]) == 0  # no jobs, but notices: next run shows them
    assert notes == ["a.mp4: 0:10, clip\nb.mp4 is empty"]
    assert list((app / "notices").glob("*")) == []


def test_main_backend_too_old_notifies_and_keeps_job(app, tmp_path, monkeypatch):
    notes = []
    tray(monkeypatch, notes)

    def old(*a, **k):
        raise uploader.BackendTooOld("The Mac Mini backend needs updating before the uploader can work", 401)

    monkeypatch.setattr(uploader, "run_job", old)
    uploader.enqueue([str(make_file(tmp_path))], app, lambda p: None)
    assert uploader.main(["--resume"]) == 0
    assert notes == ["The Mac Mini backend needs updating before the uploader can work"]
    assert len(queued(app)) == 1


def test_main_tray_failure_never_drops_a_job(app, tmp_path, monkeypatch):
    notes = []
    Base = tray(monkeypatch, notes)

    class BrokenIcon(Base):
        def __setattr__(self, k, v):
            if k == "title" and hasattr(self, "stopped"):
                raise ValueError("string too long")
            super().__setattr__(k, v)

    tray(monkeypatch, notes, BrokenIcon)
    seen = []

    def job_with_progress(job, backend, http, gate, progress, *a, jobs_dir, **k):
        progress("vod.mp4", 50, 100)
        progress("vod.mp4", 50, 100, paused=True)
        uploader._forget(job, jobs_dir)
        seen.append(job["job_id"])
        return "done"

    monkeypatch.setattr(uploader, "run_job", job_with_progress)
    uploader.enqueue([str(make_file(tmp_path))], app, lambda p: None)
    assert uploader.main(["--resume"]) == 0
    assert len(seen) == 1 and queued(app) == [] and notes == []


def test_fit_cuts_by_utf16_units_without_splitting_pairs():
    assert uploader._fit("short", 128) == "short"
    cut = uploader._fit("😀" * 64, 128)  # 128 units: one too many with the terminator
    assert cut == "😀" * 63
    cut = uploader._fit("a" + "😀" * 200, 256)
    assert len(cut.encode("utf-16-le")) // 2 <= 255
    cut.encode("utf-16-le")  # no lone surrogate (strict encode would raise)


def test_main_tray_failure_before_drain_still_releases_lock(app, tmp_path, monkeypatch):
    notes = []
    Base = tray(monkeypatch, notes)

    class NoShow(Base):
        def __setattr__(self, k, v):
            if k == "visible":
                raise OSError("no tray")
            super().__setattr__(k, v)

    tray(monkeypatch, notes, NoShow)
    uploader.enqueue([str(make_file(tmp_path))], app, lambda p: None)
    assert uploader.main(["--resume"]) == 0
    held = uploader.try_lock(app)
    assert held is not None  # not left locked for ever
    uploader.unlock(held)
    assert len(queued(app)) == 1


# --- audit fixes -----------------------------------------------------------------

def test_permanent_google_4xx_drops_the_job_with_a_message(tmp_path, monkeypatch):
    f, jobs, job = enqueue_one(tmp_path)
    r = resp(403, {})
    r.reason = "Forbidden"

    def query_offset(http, url, total):
        raise requests.HTTPError("403", response=r)

    monkeypatch.setattr(uploader, "query_offset", query_offset)
    job.update(project_id="p1", upload_url="https://upload/1")
    sleep = Recorder()
    assert run(job, FakeBackend(), jobs, sleep=sleep) == "dropped: Upload to Drive failed: 403 Forbidden"
    assert sleep.calls == [] and queued(jobs) == []


def test_google_429_is_still_retried(tmp_path, monkeypatch):
    f, jobs, job = enqueue_one(tmp_path)
    r = resp(429, {})
    calls = []

    def query_offset(http, url, total):
        calls.append(1)
        if len(calls) == 1:
            raise requests.HTTPError("429", response=r)
        return {"id": "file-1"}

    monkeypatch.setattr(uploader, "query_offset", query_offset)
    job.update(project_id="p1", upload_url="https://upload/1")
    assert run(job, FakeBackend(), jobs) == "done"


def test_a_briefly_locked_job_file_is_retried_not_lost(tmp_path, monkeypatch):
    f, jobs, job = enqueue_one(tmp_path)
    monkeypatch.setattr(uploader, "_io_sleep", lambda s: None)
    real = os.replace
    n = {"fails": 3}

    def flaky(a, b):
        if n["fails"]:
            n["fails"] -= 1
            raise PermissionError("locked")
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    job["extra"] = 1
    uploader._save(job, jobs)
    assert n["fails"] == 0 and load_one(jobs)["extra"] == 1


def test_forget_survives_a_locked_file(tmp_path, monkeypatch):
    f, jobs, job = enqueue_one(tmp_path)
    monkeypatch.setattr(uploader, "_io_sleep", lambda s: None)

    def locked(self, missing_ok=False):
        raise PermissionError("locked")

    monkeypatch.setattr(uploader.Path, "unlink", locked)
    uploader._forget(job, jobs)  # must not raise
    assert len(queued(jobs)) == 1
