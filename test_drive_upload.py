import pytest

from drive_upload import CHUNK, SessionExpired, query_offset, send_from


class FakeResponse:
    def __init__(self, status, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class FakeHttp:
    """Records every PUT and replays canned responses in order."""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def put(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return self.responses.pop(0)


def ok308(last=None):
    return FakeResponse(308, {"Range": f"bytes=0-{last}"} if last is not None else {})


def test_query_offset_parses_range():
    http = FakeHttp(ok308(8388607))
    assert query_offset(http, "u", 20971520) == 8388608
    call = http.calls[0]
    assert call["headers"] == {"Content-Range": "bytes */20971520", "Content-Length": "0"}
    assert call["timeout"] == (10, 120)


def test_query_offset_no_range_is_zero():
    assert query_offset(FakeHttp(ok308()), "u", 100) == 0


def test_query_offset_done_returns_file():
    assert query_offset(FakeHttp(FakeResponse(200, body={"id": "F"})), "u", 100) == {"id": "F"}


@pytest.mark.parametrize("status", [404, 410])
def test_query_offset_404_and_410_raise_session_expired(status):
    with pytest.raises(SessionExpired):
        query_offset(FakeHttp(FakeResponse(status)), "u", 100)


def test_query_offset_other_error_raises_http_error():
    import requests
    with pytest.raises(requests.HTTPError):
        query_offset(FakeHttp(FakeResponse(500)), "u", 100)


def make_file(tmp_path, size):
    p = tmp_path / "v.mp4"
    p.write_bytes(bytes(i % 251 for i in range(size)))
    return p


def test_send_from_sends_256k_aligned_chunks_with_content_range(tmp_path):
    total = 20 * 1024 * 1024
    path = make_file(tmp_path, total)
    http = FakeHttp(ok308(CHUNK - 1), ok308(2 * CHUNK - 1), FakeResponse(201, body={"id": "FILE"}))
    assert send_from(http, "u", path, total, 0, lambda n: True) == {"id": "FILE"}
    assert [c["headers"]["Content-Range"] for c in http.calls] == [
        "bytes 0-8388607/20971520",
        "bytes 8388608-16777215/20971520",
        "bytes 16777216-20971519/20971520",
    ]
    assert all(len(c["data"]) % (256 * 1024) == 0 for c in http.calls)
    assert all(c["timeout"] == (10, 120) for c in http.calls)


def test_send_from_follows_server_range_after_short_commit(tmp_path):
    total = 2 * CHUNK
    path = make_file(tmp_path, total)
    short = 256 * 1024
    http = FakeHttp(ok308(short - 1), FakeResponse(200, body={"id": "F"}))
    send_from(http, "u", path, total, 0, lambda n: True)
    assert http.calls[1]["headers"]["Content-Range"] == f"bytes {short}-{short + CHUNK - 1}/{total}"
    assert http.calls[1]["data"] == path.read_bytes()[short:short + CHUNK]


def test_send_from_stops_when_on_chunk_false(tmp_path):
    total = 2 * CHUNK
    path = make_file(tmp_path, total)
    http = FakeHttp(ok308(CHUNK - 1))
    seen = []
    assert send_from(http, "u", path, total, 0, lambda n: seen.append(n) or False) == {}
    assert seen == [CHUNK]
    assert len(http.calls) == 1


def test_send_from_starts_at_offset(tmp_path):
    total = CHUNK + 1000
    path = make_file(tmp_path, total)
    http = FakeHttp(FakeResponse(200, body={"id": "F"}))
    send_from(http, "u", path, total, CHUNK, lambda n: True)
    assert http.calls[0]["headers"]["Content-Range"] == f"bytes {CHUNK}-{total - 1}/{total}"
    assert http.calls[0]["data"] == path.read_bytes()[CHUNK:]


@pytest.mark.parametrize("status", [404, 410])
def test_send_from_session_expired(tmp_path, status):
    path = make_file(tmp_path, 1000)
    with pytest.raises(SessionExpired):
        send_from(FakeHttp(FakeResponse(status)), "u", path, 1000, 0, lambda n: True)
