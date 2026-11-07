"""The Carvera Controller's Upload API, for the Fusion add-in. Standard library only, no Fusion imports.

Loopback only. The API has no run or command endpoints, and neither does this client.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PORT = 8642
#: The extensions the Upload API accepts (pinned to the API by tests/unit/test_fusion_addin.py).
GCODE_EXTENSIONS = (".nc", ".gcode", ".cnc", ".ngc", ".tap", ".txt")
MAX_NAME = 100
FINISHED = ("done", "failed", "cancelled")
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".carvera_fusion_addin.json")


class ApiError(Exception):
    """A message to show the user as it is."""


# The system proxy must not see requests to this computer.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_settings(path: str = SETTINGS_PATH) -> dict:
    """Port and token. They live outside the add-in folder, so a token can't end up in the repo."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    port = raw.get("port")
    token = raw.get("token")
    return {
        "port": port if isinstance(port, int) and 0 < port < 65536 else DEFAULT_PORT,
        "token": token if isinstance(token, str) else "",
    }


def save_settings(settings: dict, path: str = SETTINGS_PATH) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"port": settings["port"], "token": settings["token"]}, fh)


def safe_name(filename: str) -> str:
    """A file name the Upload API accepts. The same input always gives the same name, so posting
    again replaces the file on the machine."""
    stem, ext = os.path.splitext(os.path.basename(filename))
    if ext.lower() not in GCODE_EXTENSIONS:
        raise ApiError(f"The post wrote “{filename}”, but the Carvera takes {', '.join(GCODE_EXTENSIONS)} files.")
    # ".lz" is refused because the firmware treats those names as compressed.
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem.replace(".lz", "_lz")).strip("_") or "fusion"
    return stem[: MAX_NAME - len(ext)] + ext.lower()


def _request(settings: dict, method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{settings['port']}{path}"
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))  # noqa: S310 - http on loopback
    if settings["token"]:
        req.add_header("Authorization", f"Bearer {settings['token']}")
    try:
        with _opener.open(req, timeout=30 if body else 3) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            why = json.loads(exc.read().decode("utf-8")).get("error", f"HTTP {exc.code}")
        except ValueError:
            why = f"HTTP {exc.code}"
        if exc.code == 401:
            raise ApiError(f"The Controller wants its Upload API token ({why}). Set it in this dialog.") from exc
        raise ApiError(f"The Controller refused: {why}") from exc
    except (OSError, ValueError) as exc:
        raise ApiError(
            f"The Controller's Upload API isn't answering on port {settings['port']}. "
            "Open the Controller and turn on Settings → Upload API."
        ) from exc


def get_status(settings: dict) -> dict:
    status = _request(settings, "GET", "/v1/status")
    if status.get("api") != 1:
        raise ApiError("This Controller speaks a different Upload API version.")
    return status


def describe_status(status: dict) -> str:
    machine = status.get("machine") or {}
    if machine.get("ready"):
        return f"Ready ({machine.get('name') or 'machine connected'})"
    return f"Will wait: {machine.get('reason') or 'machine not ready'}"


def queue_upload(settings: dict, name: str, content: bytes) -> dict:
    """Queue a file. A file with this name on the machine is replaced when the upload runs."""
    query = urllib.parse.urlencode({"name": name})
    headers = {"X-Content-MD5": hashlib.md5(content).hexdigest()}  # noqa: S324 - integrity check the API requires
    return _request(settings, "POST", f"/v1/uploads?{query}", content, headers)


def get_job(settings: dict, job_id: str) -> dict:
    return _request(settings, "GET", f"/v1/uploads/{urllib.parse.quote(job_id, safe='')}")


def keep_watching(jobs: list, elapsed: float, queued_wait: float = 10, limit: float = 300) -> bool:
    """Keep showing progress? Stop when all are finished. An upload in progress is worth waiting for;
    a queue held by a busy machine isn't, because the Controller carries on without us."""
    if elapsed >= limit or all(j.get("state") in FINISHED for j in jobs):
        return False
    return elapsed < queued_wait or any(j.get("state") == "uploading" for j in jobs)


def summarize(jobs: list) -> str:
    """What happened to each file, for the closing message."""
    lines = []
    for job in jobs:
        state = job.get("state")
        if state == "done":
            lines.append(f"Uploaded to {job.get('path') or job.get('name')}")
        elif state in ("queued", "uploading"):
            lines.append(
                f"{job.get('name')}: {state}. The Controller finishes this by itself once the machine is Idle; "
                "its top bar shows the upload."
            )
        else:
            lines.append(f"{job.get('name')}: {state}. {job.get('message') or ''}".rstrip())
    return "\n".join(lines)
