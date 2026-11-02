"""Upload API: request validation, auth, origins, and the queue, against a real HTTP server on
localhost with a fake machine bridge (the Kivy bridge is exercised on hardware)."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import threading
import time

import pytest

from carveracontroller.addons.upload_api.api import (
    REMOTE_DIR,
    UploadApi,
    UploadOutcome,
    UploadQueue,
    is_loopback,
    validate_name,
)


class FakeBridge:
    def __init__(self, ready=True, outcome=UploadOutcome.OK):
        self.ready = ready
        self.reason = "machine is Run"
        self.outcome = outcome
        self.uploads: list[tuple[str, str, bytes]] = []
        self.notices: list[str] = []
        self.started = threading.Event()

    def readiness(self):
        return (True, "") if self.ready else (False, self.reason)

    def machine_name(self):
        return "10.0.0.127"

    def upload(self, local_path, remote_dir, done):
        with open(local_path, "rb") as fh:
            self.uploads.append((local_path, remote_dir, fh.read()))
        self.started.set()
        done(self.outcome, "uploaded" if self.outcome == UploadOutcome.OK else "busy or failed")

    def notify(self, message):
        self.notices.append(message)


@pytest.fixture
def make_api():
    """Start Upload APIs on a free local port (stopped at teardown); returns (api, bridge)."""
    started = []

    def make(on_change=None, **start_kwargs):
        bridge = FakeBridge()
        server = UploadApi(bridge, on_change=on_change(lambda: server) if on_change else None)
        started.append(server)
        server.start("127.0.0.1", 0, **start_kwargs)
        server.queue.poll_interval = 0.02
        return server, bridge

    yield make
    for server in started:
        server.stop()


@pytest.fixture
def api(make_api):
    return make_api()


def request(server, method, path, body=None, headers=None):
    host, port = server.address
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    data = json.loads(raw) if raw else None
    return resp.status, data, dict(resp.getheaders())


def post_file(server, name, content: bytes, md5=None, headers=None):
    h = {"Content-Type": "application/octet-stream", "X-Content-MD5": md5 or hashlib.md5(content).hexdigest()}
    h.update(headers or {})
    return request(server, "POST", f"/v1/uploads?name={name}", body=content, headers=h)


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def job_state(server, job):
    return request(server, "GET", f"/v1/uploads/{job['id']}")[1]["state"]


def wait_for_state(server, job, state):
    return wait_for(lambda: job_state(server, job) == state)


def test_names():
    assert validate_name("coin-20261101.nc") is None
    assert validate_name("a_b.gcode") is None
    for bad in ["", "../x.nc", "a/b.nc", "a b.nc", "x.lz.nc", "x.bin", "..nc", "-" * 120 + ".nc"]:
        assert validate_name(bad), bad


def test_loopback():
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("") and not is_loopback("10.0.0.5")


def test_status(api):
    server, _ = api
    status, data, _ = request(server, "GET", "/v1/status")
    assert status == 200
    assert data["api"] == 1
    assert data["machine"] == {"name": "10.0.0.127", "ready": True, "reason": ""}
    assert data["remote_dir"] == REMOTE_DIR


def test_upload_is_queued_and_sent(api):
    server, bridge = api
    content = b"G21\nG90\n" * 1000
    status, job, headers = post_file(server, "coin.nc", content)
    assert status == 202
    assert job["state"] == "queued" and job["path"] == f"{REMOTE_DIR}/coin.nc"
    assert headers["Location"] == f"/v1/uploads/{job['id']}"
    assert wait_for_state(server, job, "done")
    (path, remote_dir, sent) = bridge.uploads[0]
    assert sent == content and remote_dir == REMOTE_DIR and path.endswith("/coin.nc")
    assert "uploaded coin.nc" in bridge.notices[0]


def test_waits_until_the_machine_is_ready(api):
    server, bridge = api
    bridge.ready = False
    _, job, _ = post_file(server, "wait.nc", b"G21\n")
    time.sleep(0.2)
    assert bridge.uploads == []
    assert request(server, "GET", "/v1/status")[1]["waiting"] == "machine is Run"
    bridge.ready = True
    assert wait_for_state(server, job, "done")


def test_busy_at_the_last_moment_requeues(api):
    server, bridge = api
    bridge.outcome = UploadOutcome.BUSY
    _, job, _ = post_file(server, "again.nc", b"G21\n")
    assert bridge.started.wait(2)
    time.sleep(0.1)
    assert job_state(server, job) == "queued"
    bridge.outcome = UploadOutcome.OK
    assert wait_for_state(server, job, "done")


def test_failed_upload_is_reported(api):
    server, bridge = api
    bridge.outcome = UploadOutcome.FAILED
    _, job, _ = post_file(server, "bad.nc", b"G21\n")
    assert wait_for_state(server, job, "failed")
    assert "failed" in bridge.notices[0]


def test_md5_mismatch_is_rejected_and_nothing_queued(api):
    server, bridge = api
    status, data, _ = post_file(server, "x.nc", b"G21\n", md5="0" * 32)
    assert status == 422 and "MD5 mismatch" in data["error"]
    assert request(server, "GET", "/v1/uploads")[1]["uploads"] == []
    assert bridge.uploads == []


def test_bad_requests(api):
    server, _ = api
    assert post_file(server, "../x.nc", b"G21\n")[0] == 400
    assert post_file(server, "x.exe", b"G21\n")[0] == 400
    status, _, _ = request(server, "POST", "/v1/uploads?name=x.nc", body=b"G21\n", headers={})
    assert status == 400  # no MD5
    assert post_file(server, "x.nc", b"")[0] == 400  # empty
    assert request(server, "GET", "/v1/nope")[0] == 404
    assert request(server, "POST", "/v1/status", body=b"", headers={"Content-Length": "0"})[0] == 404
    assert request(server, "GET", "/v1/uploads/doesnotexist")[0] == 404


def test_cancel_queued(api):
    server, bridge = api
    bridge.ready = False
    _, job, _ = post_file(server, "c.nc", b"G21\n")
    status, data, _ = request(server, "DELETE", f"/v1/uploads/{job['id']}")
    assert status == 200 and data["state"] == "cancelled"
    bridge.ready = True
    time.sleep(0.2)
    assert bridge.uploads == []
    assert request(server, "DELETE", f"/v1/uploads/{job['id']}")[0] == 409


def test_token(make_api):
    server, _ = make_api(token="s3cret")
    assert request(server, "GET", "/v1/status")[0] == 401
    assert request(server, "GET", "/v1/status", headers={"Authorization": "Bearer nope"})[0] == 401
    assert request(server, "GET", "/v1/status", headers={"Authorization": "Bearer s3cret"})[0] == 200
    assert post_file(server, "t.nc", b"G21\n")[0] == 401


def test_lan_requires_a_token():
    server = UploadApi(FakeBridge())
    with pytest.raises(ValueError):
        server.start("0.0.0.0", 0)
    assert not server.running


def test_browser_origins(make_api):
    server, _ = make_api(allowed_origins=["https://app.example.com"])
    # A page from another origin can't use it, even for a "simple" request.
    status, _, _ = post_file(server, "o.nc", b"G21\n", headers={"Origin": "https://evil.example"})
    assert status == 403
    assert request(server, "OPTIONS", "/v1/uploads", headers={"Origin": "https://evil.example"})[0] == 403
    # The allowed origin gets CORS headers.
    status, _, headers = request(
        server,
        "OPTIONS",
        "/v1/uploads",
        headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST"},
    )
    assert status == 204 and headers["Access-Control-Allow-Origin"] == "https://app.example.com"
    status, _, headers = request(server, "GET", "/v1/status", headers={"Origin": "https://app.example.com"})
    assert status == 200 and headers["Access-Control-Allow-Origin"] == "https://app.example.com"
    # Non-browser clients send no Origin and are allowed.
    assert request(server, "GET", "/v1/status")[0] == 200


def test_queue_runs_jobs_in_order(tmp_path):
    bridge = FakeBridge()
    q = UploadQueue(bridge, spool_dir=str(tmp_path))
    for name in ("a.nc", "b.nc"):
        p = tmp_path / name[0] / name  # one directory per job, as the POST handler spools them
        p.parent.mkdir()
        p.write_bytes(name.encode())
        q.add(name=name, local_path=str(p), size=4, md5="x" * 32, client="test")
    q.run_once()
    q.run_once()
    assert [u[2] for u in bridge.uploads] == [b"a.nc", b"b.nc"]
    assert list(tmp_path.iterdir()) == []  # spooled files and their job directories removed


def test_top_bar_summary():
    from carveracontroller.addons.upload_api.api import summarize

    base = {
        "running": True,
        "address": ("127.0.0.1", 8642),
        "receiving": [],
        "uploading": None,
        "queued": 0,
        "waiting": "",
        "last": None,
    }
    assert summarize({"running": False}, 0)["visible"] is False
    idle = summarize(base, 100)
    assert (idle["visible"], idle["main"], idle["minor"], idle["active"]) == (True, "Upload API", "port 8642", False)
    assert "this computer" in idle["tooltip"]
    assert summarize({**base, "receiving": ["a.nc"]}, 100)["main"] == "Receiving"
    assert summarize({**base, "receiving": ["a.nc"]}, 100)["active"] is True
    sending = summarize({**base, "uploading": "a.nc"}, 100)
    assert (sending["main"], sending["minor"], sending["active"]) == ("Uploading", "a.nc", True)
    waiting = summarize({**base, "queued": 2, "waiting": "machine is Run"}, 100)
    assert (waiting["main"], waiting["minor"]) == ("2 waiting", "machine is Run")
    done = {**base, "last": {"name": "a.nc", "state": "done", "finished_at": 95, "message": ""}}
    assert summarize(done, 100)["main"] == "Uploaded"
    assert summarize(done, 100)["refresh_in"] == 5  # when "Uploaded" fades back
    assert idle["refresh_in"] is None
    assert summarize(done, 200)["main"] == "Upload API"  # fades back
    failed = {**base, "last": {"name": "a.nc", "state": "failed", "finished_at": 95, "message": "x"}}
    assert summarize(failed, 110)["main"] == "Failed"


def test_changes_are_reported_and_done_jobs_know_their_machine_path(make_api):
    changes = []
    server, _ = make_api(on_change=lambda get: lambda: changes.append(get().snapshot()))
    assert changes and changes[-1]["running"] is True
    _, job, _ = post_file(server, "seen.nc", b"G21\n")
    assert wait_for_state(server, job, "done")
    assert any(c.get("receiving") == ["seen.nc"] for c in changes)  # while the body arrived
    assert any(c.get("uploading") == "seen.nc" for c in changes)
    assert server.snapshot()["last"]["state"] == "done"
    assert request(server, "GET", f"/v1/uploads/{job['id']}")[1]["remote_path"] == f"{REMOTE_DIR}/seen.nc"
    spool = server.queue.spool_dir
    assert os.listdir(spool) == []  # the job's file and directory are gone
    server.stop()
    assert changes[-1] == {"running": False}
    assert not os.path.exists(spool)
