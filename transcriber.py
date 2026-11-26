"""
Makes Hinglish transcripts of review versions on this PC's GPU while it is idle.

Runs at login (install_uploader.py). Every OBSERVE_SECONDS it samples the GPU;
once the IdleGate is ready it asks the Mac Mini for a job at most every
POLL_SECONDS (asking is what claims one). A job is downloaded straight from
Drive, transcribed by transcribe_worker.py in a child process, and posted back.
A game, full screen, going live or the switch flipped to Paused kills the child
within ABORT_CHECK_SECONDS and hands the job back; temporary files are always
deleted.

Never log the secret or the Drive access token.
"""
import json
import logging
import logging.handlers
import subprocess
import sys
import time
from pathlib import Path

import requests

from idle_gate import DEFAULT_GAMES, NO_WINDOW, IdleGate, game_running
from region import load_config
from uploader import _STOPPED, BadSecret, Backend, Refused, _io_retry, _retry, base_url, try_lock

log = logging.getLogger("transcriber")

HERE = Path(__file__).parent
WORK_DIR = HERE / "transcripts"
MODEL_DIR = HERE / "models" / "hinglish-apex-ct2"
WORKER = HERE / "transcribe_worker.py"
POLL_SECONDS = 60
OBSERVE_SECONDS = 10
ABORT_CHECK_SECONDS = 5
STATUS_SECONDS = 30  # /status is read at most this often during a job
COMPLETE_ATTEMPTS = 5
DRIVE_MEDIA = "https://www.googleapis.com/drive/v3/files/{}?alt=media"


class TranscriberBackend(Backend):
    def _get(self, path):
        resp = self._http.get(f"{self._base}{path}", headers={"X-Agent-Secret": self._secret},
                              timeout=(10, 30))
        if resp.status_code == 401:
            raise BadSecret("the agent secret does not match the Mac Mini's, or its backend is too old", 401)
        resp.raise_for_status()
        return resp.json()

    def next(self) -> dict:
        return self._get("/api/transcriber/next")

    def status(self) -> dict:
        return self._get("/api/transcriber/status")

    def complete(self, iid, token, segments, model) -> dict:
        return self._post(f"/api/transcriber/jobs/{iid}/complete",
                          {"token": token, "segments": segments, "model": model})

    def release(self, iid, token, error=None) -> dict:
        body = {"token": token}
        if error:
            body["error"] = error
        return self._post(f"/api/transcriber/jobs/{iid}/release", body)


def status_says_stop(backend, clock=time.monotonic):
    """True once /status says Paused or live; a failed read does not stop the job."""
    st = {"at": None, "stop": False}

    def check():
        if st["at"] is None or clock() - st["at"] >= STATUS_SECONDS:
            st["at"] = clock()
            try:
                s = backend.status()
                st["stop"] = s.get("enabled") is False or s.get("live") is True
            except BadSecret:
                raise
            except Exception as e:
                log.warning("status check failed (%s)", type(e).__name__)
                st["stop"] = False
        return st["stop"]

    return check


class _Aborted(Exception):
    pass


def _download(http, job, dest, abort) -> None:
    with http.get(DRIVE_MEDIA.format(job["file_id"]), stream=True, timeout=(10, 60),
                  headers={"Authorization": f"Bearer {job['access_token']}"}) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(1 << 20):
                f.write(chunk)
                if abort():
                    raise _Aborted()


def block_list(cfg) -> list:
    """The game processes that keep the GPU ours to leave alone: a list of names, else the default."""
    v = cfg.get("transcriber_block_processes")
    if v is None:
        return list(DEFAULT_GAMES)
    if isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v):
        return v
    log.warning("transcriber_block_processes must be a list of process names; using %s", DEFAULT_GAMES)
    return list(DEFAULT_GAMES)


def run_job(job, backend, http, should_abort, spawn=subprocess.Popen, workdir=WORK_DIR,
            clock=time.monotonic, sleep=time.sleep) -> str:
    try:  # it becomes a file name: only a plain number may
        iid = int(job["iteration_id"])
    except (TypeError, ValueError):
        log.error("job with a malformed version id dropped")
        return "failed"
    if iid <= 0 or str(iid) != str(job["iteration_id"]).strip():
        log.error("job with a malformed version id dropped")
        return "failed"
    token = job["token"]
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    media, out, err = (workdir / f"{iid}{ext}" for ext in (".media", ".json", ".err"))
    last = [None]

    def abort():  # should_abort can shell out; ask at most every ABORT_CHECK_SECONDS
        if last[0] is not None and clock() - last[0] < ABORT_CHECK_SECONDS:
            return False
        last[0] = clock()
        return should_abort()

    def release(error=None):
        try:
            backend.release(iid, token, error)
        except Exception as e:  # unreachable: the backend re-queues a stale claim itself
            log.warning("release of %s failed (%s)", iid, type(e).__name__)
        return "failed" if error else "released"

    try:
        log.info("version %s: downloading", iid)
        try:
            _download(http, job, media, abort)
        except _Aborted:
            log.info("version %s: stopped during download; handed back", iid)
            return release()
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            log.warning("version %s: download failed (%s %s)", iid, type(e).__name__, status)
            return release(f"Download from Drive failed ({status or type(e).__name__})")

        log.info("version %s: transcribing", iid)
        started = clock()
        with open(err, "wb") as err_fh:
            proc = spawn([sys.executable, str(WORKER), str(media), str(out), str(MODEL_DIR)],
                         stdout=subprocess.DEVNULL, stderr=err_fh, creationflags=NO_WINDOW)
            while True:
                try:
                    code = proc.wait(timeout=ABORT_CHECK_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    if abort():
                        proc.kill()
                        proc.wait()
                        log.info("version %s: stopped (game, full screen, live or paused); handed back", iid)
                        return release()
        if code != 0:
            tail = err.read_text(encoding="utf-8", errors="replace")[-2000:]
            log.error("version %s: worker exited %s:\n%s", iid, code, tail)
            return release(f"The transcriber crashed (exit code {code})")
        data = json.loads(out.read_text(encoding="utf-8"))
        log.info("version %s: %d lines in %.0f s", iid, len(data["segments"]), clock() - started)

        tries = {"attempt": 0}
        try:
            r = _retry(lambda: backend.complete(iid, token, data["segments"], data["model"]), sleep,
                      lambda: tries["attempt"] >= COMPLETE_ATTEMPTS, tries)
        except BadSecret:
            raise
        except Refused as e:
            if e.status == 409:  # re-claimed or deleted meanwhile: the new claim wins
                log.info("version %s: backend no longer wants this transcript; dropped", iid)
                return "released"
            log.error("version %s: backend refused the transcript (%s)", iid, e.status)
            return release(f"The backend refused the transcript ({e.status})")
        if r is _STOPPED:
            log.error("version %s: could not reach the backend to post the transcript", iid)
            return "failed"
        log.info("version %s: done", iid)
        return "done"
    finally:
        for p in (media, out, err, out.with_suffix(".tmp")):
            try:
                _io_retry(lambda: p.unlink(missing_ok=True))
            except OSError as e:
                log.warning("could not delete %s (%s)", p.name, type(e).__name__)


def loop(gate, backend, run, stop, sleep=time.sleep, clock=time.monotonic) -> None:
    last_poll = None
    while not stop():
        gate.observe()
        if gate.ready() and (last_poll is None or clock() - last_poll >= POLL_SECONDS):
            last_poll = clock()
            try:
                job = (backend.next() or {}).get("job")
            except BadSecret:
                raise
            except Exception as e:
                log.warning("asking for a job failed (%s)", type(e).__name__)
                job = None
            if job:
                run(job)
        sleep(OBSERVE_SECONDS)


def _setup_logging() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(WORK_DIR / "transcriber.log", maxBytes=1_000_000,
                                             backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def main() -> int:
    _setup_logging()
    try:
        cfg = load_config()
        base, secret = base_url(cfg["backend_url"]), cfg["agent_secret"]
        if not secret:
            raise ValueError("agent_secret is empty")
    except Exception as e:
        log.error("agent_config.json is missing or invalid (%s)", type(e).__name__)
        return 1
    if not MODEL_DIR.is_dir():
        log.error("model folder %s is missing; see UPLOADER_SETUP.md", MODEL_DIR)
        return 1
    lock = try_lock(WORK_DIR)
    if lock is None:
        return 0  # already running
    games = block_list(cfg)
    http = requests.Session()
    backend = TranscriberBackend(base, secret, http)
    gate = IdleGate(game=lambda: game_running(games))
    log.info("started")

    def run(job):
        check = status_says_stop(backend)
        run_job(job, backend, http, lambda: gate.interrupt() or check())

    while True:
        try:
            loop(gate, backend, run, stop=lambda: False)
        except BadSecret as e:
            log.error("%s; retrying in 10 minutes", e.message)
            time.sleep(600)
        except Exception:
            log.exception("transcriber loop crashed; restarting in a minute")
            time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
