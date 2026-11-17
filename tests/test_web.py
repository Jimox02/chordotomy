import json
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from chordotomy.web import Session, make_handler


@pytest.fixture
def server():
    session = Session()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(session))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, session
    session.stop()
    if session.thread:
        session.thread.join(2)
    server.shutdown()
    server.server_close()
    thread.join(2)


def request(server, method, path, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    default = {"Content-Type": "application/json"}
    default.update(headers or {})
    conn.request(method, path, json.dumps(body) if body is not None else None, default)
    response = conn.getresponse()
    result = response.status, response.read()
    conn.close()
    return result


def test_dashboard_and_demo_lifecycle(server):
    http, session = server
    status, body = request(http, "GET", "/")
    assert status == 200
    assert "Harmonic Atlas" in body.decode()
    assert request(http, "POST", "/api/start", {"demo": True})[0] == 200
    assert request(http, "POST", "/api/start", {"demo": True})[0] == 400
    deadline = time.monotonic() + 4
    while (
        not session.snapshot()["latest"] or not session.snapshot()["latest"]["stable"]
    ) and time.monotonic() < deadline:
        time.sleep(0.05)
    status, body = request(http, "GET", "/api/state")
    state = json.loads(body)
    assert state["status"] == "demo"
    assert state["latest"]["chord"] == "C:maj"
    assert request(http, "POST", "/api/stop", {})[0] == 200
    session.thread.join(2)
    assert session.snapshot()["status"] == "stopped"
    assert request(http, "POST", "/api/start", {"demo": True})[0] == 200
    assert session.snapshot()["session"] == 2


def test_foreign_origin_and_dns_host_rejected(server):
    http, session = server
    assert (
        request(http, "POST", "/api/start", {"demo": True}, {"Origin": "https://example.org"})[0]
        == 403
    )
    assert request(http, "GET", "/api/state", headers={"Host": "example.org"})[0] == 403
    assert session.thread is None


def test_invalid_request_and_capture_error(server, monkeypatch):
    import chordotomy.web as web

    http, session = server
    assert request(http, "POST", "/api/start", {"demo": "yes"})[0] == 400
    assert request(http, "GET", "/missing")[0] == 404
    monkeypatch.setattr(
        web, "Capture", lambda *_, **__: (_ for _ in ()).throw(RuntimeError("device error"))
    )
    assert request(http, "POST", "/api/start", {})[0] == 200
    session.thread.join(2)
    state = session.snapshot()
    assert state["status"] == "error"
    assert "device error" in state["error"]


def test_web_engine_selection_validation(server, monkeypatch):
    import chordotomy.web as web

    http, session = server
    requested = []

    def detector(engine):
        requested.append(engine)
        raise RuntimeError("model missing")

    monkeypatch.setattr(web, "make_detector", detector)
    assert request(http, "POST", "/api/start", {"engine": "invalid"})[0] == 400
    assert session.thread is None
    assert request(http, "POST", "/api/start", {"engine": "model"})[0] == 200
    session.thread.join(2)
    assert requested == ["model"]
    assert session.snapshot()["engine"] == "model"
    assert session.snapshot()["error"] == "model missing"
