"""
"Send to editor": a durable job queue on disk plus the one worker that drains it.

Explorer starts one process per selected file at the same moment. Each writes
its job file FIRST and only then tries the lock; the winner drains the queue and,
when it looks empty, unlocks and looks once more. So a loser's job is either seen
by that re-check or the loser wins the lock itself - nothing is lost in between.

A job is `uploads/<job_id>.json`. Every step that matters is saved before moving
on, so a power cut resumes where it stopped: the backend is idempotent by job_id,
Google remembers the byte offset, and `complete` is safe to repeat.

Never log the secret or an upload URL (the URL alone authorises the upload).
"""
import datetime
import json
import logging
import logging.handlers
import mimetypes
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests

from drive_upload import SessionExpired, query_offset, send_from
from live_gate import RECHECK_SECONDS, LiveGate, backend_live_from, obs_streaming_from
from region import load_config

log = logging.getLogger("uploader")

JOBS_DIR = Path(__file__).parent / "uploads"
_STOPPED = object()


class _Changed(Exception):
    """The file on disk is no longer the one the backend was told about."""


def backoff(attempt: int) -> float:
    return min(5 * 2 ** attempt, 300)


def media_duration(path):
    try:
        from pymediainfo import MediaInfo  # Windows-side dependency; imported lazily

        ms = MediaInfo.parse(path).general_tracks[0].duration
        return float(ms) / 1000 if ms else None
    except Exception:
        return None  # the backend treats an unknown duration as a VOD


# --- the queue ---------------------------------------------------------------

def _job_file(job, jobs_dir) -> Path:
    return Path(jobs_dir) / f"{job['job_id']}.json"


def _save(job, jobs_dir) -> None:
    path = _job_file(job, jobs_dir)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _forget(job, jobs_dir) -> None:
    _job_file(job, jobs_dir).unlink(missing_ok=True)


def _pending(jobs_dir) -> list:
    jobs = []
    for p in Path(jobs_dir).glob("*.json"):
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
            if not all(k in job for k in ("job_id", "path", "size", "mtime")):
                raise ValueError("missing fields")
        except FileNotFoundError:
            continue  # finished by someone else between glob and read
        except ValueError as e:  # corrupt JSON or missing fields: quarantine it
            log.error("corrupt job %s (%s); renamed to .bad", p.name, e)
            try:
                os.replace(p, p.with_name(p.name + ".bad"))
            except OSError:
                pass  # another process already moved it
            continue
        except OSError as e:  # e.g. locked by antivirus for a moment: try next pass
            log.warning("could not read job %s (%s); skipped", p.name, type(e).__name__)
            continue
        jobs.append(job)
    return sorted(jobs, key=lambda j: (j.get("created_at", 0), os.path.basename(j["path"])))


def enqueue(paths, jobs_dir=JOBS_DIR, duration=media_duration) -> list:
    """Queue each file; returns messages for the ones refused."""
    Path(jobs_dir).mkdir(parents=True, exist_ok=True)
    have = {(j["path"], j["size"], j["mtime"]) for j in _pending(jobs_dir)}
    refused = []
    for raw in paths:
        path = os.path.abspath(raw)
        name = os.path.basename(path)
        try:
            st = os.stat(path)
        except OSError:
            refused.append(f"{name} was not found")
            continue
        if st.st_size == 0:
            refused.append(f"{name} is empty")
            continue
        if (path, st.st_size, st.st_mtime) in have:
            refused.append(f"{name} is already queued")
            continue
        job = {
            "job_id": uuid.uuid4().hex,
            "path": path,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "duration_s": duration(path),
            "stream_date": datetime.date.fromtimestamp(st.st_mtime).isoformat(),
            "mime": mimetypes.guess_type(path)[0] or "video/mp4",
            "created_at": time.time(),
        }
        _save(job, jobs_dir)
        have.add((path, st.st_size, st.st_mtime))
        log.info("queued %s (%d bytes)", name, st.st_size)
    return refused


# --- the lock (the OS drops it when the process dies) ---------------------------

def try_lock(jobs_dir):
    Path(jobs_dir).mkdir(parents=True, exist_ok=True)
    fh = open(Path(jobs_dir) / ".lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def unlock(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


# --- the backend --------------------------------------------------------------

class Refused(Exception):
    """The backend said no for good: drop the job with this message."""
    def __init__(self, message, status):
        super().__init__(message)
        self.message, self.status = message, status


class BadSecret(Refused):
    """401: every job would fail the same way, so the worker stops."""


class BackendTooOld(BadSecret):
    """A 401 that is not the uploader routes' own: the Mac Mini predates them."""


SECRET_REFUSED = "Invalid or missing agent secret"  # uploader_api.py's own 401 text


def _gb(n) -> str:
    return f"{n / 1e9:.1f} GB"


class Backend:
    def __init__(self, base_url, secret, http):
        self._base, self._secret, self._http = base_url.rstrip("/"), secret, http

    def _post(self, path, body, name=""):
        resp = self._http.post(f"{self._base}{path}", json=body,
                               headers={"X-Agent-Secret": self._secret}, timeout=(10, 60))
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code >= 500 and resp.status_code != 507:
            resp.raise_for_status()  # temporary: HTTPError is a RequestException
        try:
            data = resp.json()
        except ValueError:
            data = {}
        msg = data.get("error") or f"the backend refused it ({resp.status_code})"
        if resp.status_code == 401:
            if data.get("error") == SECRET_REFUSED:
                raise BadSecret("the agent secret does not match the Mac Mini's", 401)
            # e.g. the auth middleware's "Not authenticated": the route is not in OPEN_PATHS
            raise BackendTooOld("The Mac Mini backend needs updating before the uploader can work", 401)
        if resp.status_code == 507 and "needed" in data and "free" in data:
            msg = f"Drive is too full for {name}: needs {_gb(data['needed'])}, {_gb(data['free'])} free"
        raise Refused(msg, resp.status_code)

    def register(self, job, new_session=False) -> dict:
        name = os.path.basename(job["path"])
        body = {k: job[k] for k in ("job_id", "size", "mime", "duration_s", "stream_date")}
        body["filename"] = name
        if new_session:
            body["new_session"] = True
        return self._post("/api/uploader/projects", body, name)

    def complete(self, project_id, file_id) -> dict:
        return self._post(f"/api/uploader/projects/{project_id}/complete", {"file_id": file_id})


# --- the worker ---------------------------------------------------------------

def _retry(step, sleep, stop, state):
    """Run step until it stops raising temporary errors; _STOPPED if told to stop."""
    while True:
        try:
            return step()
        except requests.RequestException as e:
            # Exception text can carry the upload URL: log only the kind and status.
            status = getattr(getattr(e, "response", None), "status_code", None)
            log.warning("%s (%s); retry %d", type(e).__name__, status, state["attempt"] + 1)
        if stop():
            return _STOPPED
        sleep(backoff(state["attempt"]))
        state["attempt"] += 1


def run_job(job, backend, http, gate, progress, sleep=time.sleep, stop=lambda: False,
            jobs_dir=JOBS_DIR) -> str:
    path, size = job["path"], job["size"]
    name = os.path.basename(path)

    def drop(reason):
        _forget(job, jobs_dir)
        log.warning("dropped %s: %s", name, reason)
        return f"dropped: {reason}"

    def done():
        _forget(job, jobs_dir)
        log.info("done %s", name)
        return "done"

    changed = f"{name} changed or moved since it was sent; send it again"

    def unchanged():
        # Never upload different bytes than the backend was told about.
        try:
            st = os.stat(path)
            return st.st_size == size and st.st_mtime == job["mtime"]
        except OSError:
            return False

    if not unchanged():
        return drop(changed)

    def register(new_session=False):
        r = _retry(lambda: backend.register(job, new_session=new_session), sleep, stop,
                   {"attempt": 0})
        if r is _STOPPED or r["status"] == "ready":
            return r
        job.update(project_id=r["project_id"], upload_url=r["upload_url"])
        _save(job, jobs_dir)
        return r

    up = {"attempt": 0, "best": -1}

    def on_chunk(offset):
        if offset > up["best"]:  # progress made: the line is working again
            up["best"], up["attempt"] = offset, 0
        progress(name, offset, size)
        paused = False
        while gate.should_pause():
            paused = True
            progress(name, offset, size, paused=True)
            if stop():
                return False
            sleep(RECHECK_SECONDS)
        if paused and not unchanged():  # edited while we waited: send no more of it
            raise _Changed()
        return not stop()

    def upload():
        got = query_offset(http, job["upload_url"], size)  # a fresh session answers 0
        if isinstance(got, dict):
            return got  # Google already has every byte
        if not on_chunk(got):
            return {}
        if not unchanged():  # checked again after any pause and on every retry
            raise _Changed()
        return send_from(http, job["upload_url"], path, size, got, on_chunk)

    try:
        if not (job.get("project_id") and job.get("upload_url")):
            r = register()
            if r is _STOPPED:
                return "stopped"
            if r["status"] == "ready":
                return done()
        log.info("uploading %s", name)
        while True:
            try:
                f = _retry(upload, sleep, stop, up)
                break
            except SessionExpired:
                log.info("upload session for %s expired; starting a new one", name)
                r = register(new_session=True)
                if r is _STOPPED:
                    return "stopped"
                if r["status"] == "ready":
                    return done()
                up["best"] = -1
        if f is _STOPPED or not f:
            return "stopped"
        if _retry(lambda: backend.complete(job["project_id"], f["id"]), sleep, stop,
                  {"attempt": 0}) is _STOPPED:
            return "stopped"
        return done()
    except BadSecret:
        raise
    except Refused as e:
        return drop(e.message)
    except _Changed:
        return drop(changed)
    except Exception as e:
        # Anything else would stall the whole queue on this one job.
        log.exception("unexpected %s sending %s", type(e).__name__, name)
        return drop(f"{name} could not be sent ({type(e).__name__}); send it again")


def drain(jobs_dir, lock_fh, run, after_unlock=lambda: None) -> list:
    """Run every job oldest-first while holding the lock; ends with it released."""
    results = []
    try:
        while True:
            jobs = _pending(jobs_dir)
            for job in jobs:
                results.append(run(job, jobs_dir=jobs_dir))
                if results[-1] == "stopped":
                    return results
            if jobs:
                continue
            fh, lock_fh = lock_fh, None
            unlock(fh)
            after_unlock()
            if not _pending(jobs_dir):
                return results
            lock_fh = try_lock(jobs_dir)
            if lock_fh is None:
                return results  # a newer sender holds it and will drain
    finally:
        if lock_fh is not None:
            unlock(lock_fh)


# --- the app: tray icon, sleep prevention, entry point ----------------------------

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
CLIP_MAX_SECONDS = 300  # the backend's editor_store.CLIP_MAX_SECONDS decides; this only labels
NOTIFY_LINGER_SECONDS = 8  # removing the icon removes its last notification with it


def _clock(seconds) -> str:
    s = int(seconds)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def summary_line(path, duration_s) -> str:
    name = os.path.basename(path.replace("\\", "/"))
    if duration_s is None:
        return f"{name}: length unknown, VOD"
    kind = "clip" if duration_s < CLIP_MAX_SECONDS else "VOD"
    return f"{name}: {_clock(duration_s)}, {kind}"


def base_url(backend_url: str) -> str:
    u = urlsplit(backend_url)
    return f"{u.scheme}://{u.netloc}"


def _post_notice(lines, jobs_dir) -> None:
    """Leave lines for whichever process holds the tray (maybe a later one)."""
    d = Path(jobs_dir) / "notices"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{uuid.uuid4().hex}.txt"
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _notices(jobs_dir) -> list:
    return sorted((Path(jobs_dir) / "notices").glob("*.txt"), key=lambda p: p.stat().st_mtime)


def _take_notices(jobs_dir) -> list:
    texts = []
    for p in _notices(jobs_dir):
        try:
            texts.append(p.read_text(encoding="utf-8"))
            p.unlink()
        except OSError:
            continue  # taken by another pass, or locked for a moment: next pass
    return texts


def _fit(text, n) -> str:
    """Fit Windows' WCHAR[n] (terminator included) without splitting a surrogate pair."""
    units = text.encode("utf-16-le")
    if len(units) // 2 < n:
        return text
    return units[: 2 * (n - 1)].decode("utf-16-le", errors="ignore")


def _setup_logging() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(JOBS_DIR / "uploader.log", maxBytes=1_000_000,
                                             backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def _icon_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (64, 64), (20, 160, 150))
    ImageDraw.Draw(img).polygon([(32, 8), (56, 34), (40, 34), (40, 56), (24, 56), (24, 34),
                                 (8, 34)], fill="white")  # an upward arrow
    return img


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    _setup_logging()
    try:
        cfg = load_config()
        base, secret = base_url(cfg["backend_url"]), cfg["agent_secret"]
        if not secret:
            raise ValueError("agent_secret is empty")
    except Exception as e:
        log.error("agent_config.json is missing or invalid (%s)", type(e).__name__)
        return 1

    files = [a for a in argv if a != "--resume"]
    lines = []
    if files:
        durations = {}

        def duration(path):
            durations[path] = media_duration(path)
            return durations[path]

        refused = enqueue(files, JOBS_DIR, duration)
        lines = [summary_line(p, d) for p, d in durations.items()] + refused
        if lines:
            _post_notice(lines, JOBS_DIR)  # before the lock: the winner shows everyone's
    if not files and not _pending(JOBS_DIR) and not _notices(JOBS_DIR):
        return 0
    lock_fh = try_lock(JOBS_DIR)
    if lock_fh is None:
        return 0  # another instance holds the queue and will send these too

    try:
        import pystray  # Windows-side dependency; imported lazily
    except ImportError:
        log.error("pystray is not installed: run pip install -r requirements.txt")
        unlock(lock_fh)
        return 1

    http = requests.Session()
    backend = Backend(base, secret, http)
    gate = LiveGate(obs_streaming_from(cfg), backend_live_from(base, secret, http))
    stop = threading.Event()
    count = {"n": 0, "total": len(_pending(JOBS_DIR))}
    last_notify = [0.0]
    checked = [0.0]

    def notify(text):
        try:  # a tray hiccup must never drop a job or stop the drain
            icon.notify(_fit(text, 256), "Send to editor")
            last_notify[0] = time.monotonic()
        except Exception as e:
            log.warning("tray notification failed (%s)", type(e).__name__)

    def show_notices():
        checked[0] = time.monotonic()
        texts = _take_notices(JOBS_DIR)
        if texts:
            notify("\n".join(texts))

    def progress(name, offset, size, paused=False):
        try:
            pct = int(offset * 100 / size) if size else 100
            if paused:
                title = f"Paused while live: {name} {pct}%"
            else:
                title = f"Sending to editor: {name} {pct}% ({count['n']} of {count['total']})"
            icon.title = _fit(title, 128)  # Windows' tooltip limit
            if time.monotonic() - checked[0] >= 5:
                show_notices()
        except Exception as e:
            log.warning("tray update failed (%s)", type(e).__name__)

    def run(job, jobs_dir):
        show_notices()
        count["n"] += 1
        count["total"] = max(count["n"], count["n"] - 1 + len(_pending(jobs_dir)))
        result = run_job(job, backend, http, gate, progress, sleep=stop.wait,
                         stop=stop.is_set, jobs_dir=jobs_dir)
        if result.startswith("dropped: "):
            notify(result[len("dropped: "):])
        return result

    def work(icon):
        draining = False
        try:
            icon.visible = True
            if sys.platform == "win32":
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            show_notices()
            draining = True
            drain(JOBS_DIR, lock_fh, run)  # releases the lock however it ends
        except BackendTooOld as e:
            notify(e.message)
        except BadSecret:
            notify("The agent secret is wrong - check agent_config.json")
        except Exception:
            log.exception("uploader stopped unexpectedly")
        finally:
            if not draining:
                unlock(lock_fh)
            if sys.platform == "win32":
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            stop.wait(max(0.0, last_notify[0] + NOTIFY_LINGER_SECONDS - time.monotonic()))
            icon.stop()

    menu = pystray.Menu(pystray.MenuItem("Stop (resumes at next login)",
                                         lambda icon, item: stop.set()))
    icon = pystray.Icon("dualbladex-uploader", _icon_image(), "Send to editor", menu)
    icon.run(setup=work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
