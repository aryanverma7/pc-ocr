import datetime
import json
import os

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
    http = FakeHttp(resp(401, {"error": "unauthorized"}))
    with pytest.raises(uploader.BadSecret):
        run(job, uploader.Backend("http://mini:8765", "s", http), jobs)
    assert len(queued(jobs)) == 1


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
