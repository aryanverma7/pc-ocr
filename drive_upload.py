"""
Google Drive resumable-upload protocol, pure: the backend hands the PC a
session URL, and these two calls move bytes to it. No retries here - the
caller (uploader.py) owns backoff and decides what a failure means.

`http` is a requests.Session-like object, injected so tests need no network.
"""

CHUNK = 8 * 1024 * 1024  # must be a multiple of 256 KiB (Google's rule)
TIMEOUT = (10, 120)


class SessionExpired(Exception):
    """Google forgot the session (~a week old): ask the backend for a new one."""


def _next_offset(resp) -> int:
    # "Range: bytes=0-N" means N+1 bytes are committed; no header means none.
    rng = resp.headers.get("Range")
    return int(rng.rsplit("-", 1)[1]) + 1 if rng else 0


def _check(resp) -> None:
    if resp.status_code in (404, 410):
        raise SessionExpired(f"upload session gone ({resp.status_code})")


def query_offset(http, url, total):
    """Next byte Google wants, or the file's JSON dict if it already has it all."""
    resp = http.put(
        url,
        data=b"",
        headers={"Content-Range": f"bytes */{total}", "Content-Length": "0"},
        timeout=TIMEOUT,
    )
    _check(resp)
    if resp.status_code in (200, 201):
        return resp.json()
    if resp.status_code == 308:
        return _next_offset(resp)
    resp.raise_for_status()
    raise RuntimeError(f"unexpected status {resp.status_code}")


def send_from(http, url, path, total, offset, on_chunk):
    """Upload path[offset:] chunk by chunk; on_chunk(next_offset) -> False stops with {}."""
    with open(path, "rb") as f:
        while True:
            f.seek(offset)
            data = f.read(CHUNK)
            end = offset + len(data) - 1
            resp = http.put(
                url,
                data=data,
                headers={"Content-Range": f"bytes {offset}-{end}/{total}"},
                timeout=TIMEOUT,
            )
            _check(resp)
            if resp.status_code in (200, 201):
                return resp.json()
            if resp.status_code != 308:
                resp.raise_for_status()
                raise RuntimeError(f"unexpected status {resp.status_code}")
            # Trust the server's count, not what we sent.
            offset = _next_offset(resp)
            if not on_chunk(offset):
                return {}
