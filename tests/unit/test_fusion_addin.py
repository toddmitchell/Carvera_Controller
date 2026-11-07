"""The Fusion add-in's Upload API client, against the real API server on localhost with a fake machine
bridge. SendToCarvera.py itself needs Fusion and isn't covered here."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import time
from pathlib import Path

import pytest

from carveracontroller.addons.upload_api.api import GCODE_EXTENSIONS, REMOTE_DIR, UploadApi, validate_name
from tests.unit.test_upload_api import FakeBridge

_PATH = Path(__file__).parents[2] / "integrations/fusion360/SendToCarvera/carvera_api.py"
_spec = importlib.util.spec_from_file_location("carvera_api", _PATH)
assert _spec and _spec.loader
client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(client)

_spec = importlib.util.spec_from_file_location("selection", _PATH.with_name("selection.py"))
assert _spec and _spec.loader
selection = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(selection)

TREE = [(1, [11, 12, 13]), (2, [21, 22]), (3, [])]


@pytest.fixture
def running():
    """Returns (settings, bridge) for an API started with these options."""
    servers = []

    def start(ready=True, token=""):
        bridge = FakeBridge(ready=ready)
        server = UploadApi(bridge)
        servers.append(server)
        server.start("127.0.0.1", 0, token=token)
        server.queue.poll_interval = 0.02
        return {"port": server.address[1], "token": token}, bridge

    yield start
    for server in servers:
        server.stop()


def wait_for_state(settings, job_id, state):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get_job(settings, job_id)
        if job["state"] == state:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job never reached {state}: {job}")


def test_extensions_match_the_api():
    assert tuple(sorted(client.GCODE_EXTENSIONS)) == tuple(sorted(GCODE_EXTENSIONS))


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("part1.cnc", "part1.cnc"),
        ("Rev 2: top (final).CNC", "Rev_2_top_final.cnc"),
        ("a..b.nc", "a_b.nc"),
        ("job.lz.nc", "job_lz.nc"),
        ("___.nc", "fusion.nc"),
        ("日本語.nc", "fusion.nc"),
        ("x" * 300 + ".gcode", "x" * 94 + ".gcode"),
    ],
)
def test_safe_name_is_always_accepted_by_the_api(given, expected):
    name = client.safe_name(given)
    assert name == expected
    assert validate_name(name) is None


def test_safe_name_refuses_files_the_carvera_does_not_take():
    with pytest.raises(client.ApiError, match="takes"):
        client.safe_name("setup-sheet.html")


@pytest.mark.parametrize(
    ("typed", "extension", "expected"),
    [
        ("Coin rev 2", "cnc", "Coin_rev_2.cnc"),
        ("coin.nc", ".cnc", "coin.cnc"),
        ("v1.2", "cnc", "v1_2.cnc"),
        ("  ", "nc", "fusion.nc"),
    ],
)
def test_machine_name(typed, extension, expected):
    name = client.machine_name(typed, extension)
    assert name == expected
    assert validate_name(name) is None


def test_upload_reaches_the_machine_with_the_exact_bytes(running):
    settings, bridge = running()
    content = b"G21\nG90\nM5\n"
    job = client.queue_upload(settings, "part1.cnc", content)
    assert job["md5"] == hashlib.md5(content).hexdigest()  # noqa: S324
    finished = wait_for_state(settings, job["id"], "done")
    assert bridge.uploads[0][1:] == (REMOTE_DIR, content)
    assert "part1.cnc" in client.summarize([{**finished, "path": f"{REMOTE_DIR}/part1.cnc"}])


def test_posting_again_targets_the_same_file(running):
    settings, bridge = running()
    for content in (b"G0 X1\n", b"G0 X2\n"):
        wait_for_state(settings, client.queue_upload(settings, "part1.cnc", content)["id"], "done")
    assert [Path(path).name for path, _, _ in bridge.uploads] == ["part1.cnc", "part1.cnc"]
    assert [body for _, _, body in bridge.uploads] == [b"G0 X1\n", b"G0 X2\n"]


def test_busy_machine_keeps_the_job_queued_and_says_why(running):
    settings, bridge = running(ready=False)
    status = client.get_status(settings)
    assert client.describe_status(status) == "Will wait: machine is Run"
    job = client.queue_upload(settings, "part1.cnc", b"G0 X1\n")
    time.sleep(0.1)
    assert client.get_job(settings, job["id"])["state"] == "queued"
    assert not bridge.uploads


def test_token_is_sent_and_a_wrong_one_is_explained(running):
    settings, _ = running(token="secret")  # noqa: S106
    assert client.get_status(settings)["api"] == 1
    with pytest.raises(client.ApiError, match="token"):
        client.get_status({**settings, "token": "wrong"})


def test_refusal_shows_the_apis_reason(running):
    settings, _ = running()
    with pytest.raises(client.ApiError, match="must end with"):
        client.queue_upload(settings, "part1.exe", b"G0\n")


def test_controller_not_running_says_how_to_fix_it(running):
    settings, _ = running()
    closed = UploadApi(FakeBridge())
    closed.start("127.0.0.1", 0)
    port = closed.address[1]
    closed.stop()
    with pytest.raises(client.ApiError, match="Upload API"):
        client.get_status({**settings, "port": port})


@pytest.mark.parametrize(
    ("states", "elapsed", "expected"),
    [
        (["done"], 0, False),
        (["done", "failed"], 0, False),
        (["queued"], 1, True),
        (["queued"], 10, False),
        (["uploading"], 200, True),
        (["uploading"], 300, False),
        (["done", "uploading"], 50, True),
    ],
)
def test_keep_watching(states, elapsed, expected):
    assert client.keep_watching([{"state": s} for s in states], elapsed) is expected


def test_settings_are_private_and_bad_files_fall_back(tmp_path):
    path = tmp_path / "settings.json"
    defaults = {"port": client.DEFAULT_PORT, "token": "", "post": "", "documents": {}}
    assert client.load_settings(str(path)) == defaults
    saved = {
        "port": 9000,
        "token": "abc",
        "post": "Makera Carvera",
        "documents": {"doc1": {"name": "coin", "source": 5, "operations": [1]}},
    }
    client.save_settings(saved, str(path))
    assert client.load_settings(str(path)) == saved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text(json.dumps({"port": "nope", "token": 3, "post": 1, "documents": ["x"]}))
    assert client.load_settings(str(path)) == defaults


def test_remembered_ignores_bad_entries():
    settings = {"documents": {"a": {"name": "coin", "source": 7, "operations": [1, "x"]}, "b": "junk"}}
    assert client.remembered(settings, "a") == {"name": "coin", "source": 7, "operations": [1]}
    empty = {"name": "", "source": None, "operations": []}
    assert client.remembered(settings, "b") == empty
    assert client.remembered(settings, "missing") == empty


def test_expand_keeps_a_setup_and_its_toolpaths_in_step():
    assert selection.expand(TREE, [2]) == {2, 21, 22}
    assert selection.expand(TREE, [21, 22]) == {2, 21, 22}
    assert selection.expand(TREE, [11]) == {11}
    assert selection.expand(TREE, [99]) == set()
    assert selection.expand(TREE, [3]) == {3}


def test_browser_selection_beats_the_last_send():
    assert selection.initial(TREE, [12], [21]) == {12}


def test_last_send_is_used_when_nothing_is_selected():
    assert selection.initial(TREE, [], [1, 21]) == {1, 11, 12, 13, 21}


def test_toolpaths_that_no_longer_exist_fall_through():
    assert selection.initial(TREE, [99], [98]) == set()
    assert selection.initial([(1, [11])], [99], [98]) == {1, 11}


def test_a_lone_setup_starts_ticked_but_several_do_not():
    assert selection.initial([(1, [11, 12])], [], []) == {1, 11, 12}
    assert selection.initial(TREE, [], []) == set()


def test_send_uses_document_order_and_whole_setups():
    assert selection.to_send(TREE, {22, 12, 11}) == [11, 12, 22]
    assert selection.to_send(TREE, selection.expand(TREE, [1]) | {21}) == [1, 21]
    assert selection.to_send(TREE, set()) == []


NAMES = {1: "Setup1", 11: "Face1", 12: "Trace 2", 13: "Drill3", 2: "Setup2", 21: "Pocket", 22: "Bore", 3: "Empty"}


def stem(ticked):
    return selection.file_stem("Coin", TREE, selection.expand(TREE, ticked), NAMES)


def test_everything_ticked_keeps_the_plain_name():
    assert stem([1, 2]) == "Coin"
    assert selection.file_stem("Coin", [(1, [11])], {1, 11}, NAMES) == "Coin"


def test_one_toolpath_is_added_to_the_name():
    assert stem([12]) == "Coin_Trace 2"


def test_several_toolpaths_join_in_document_order():
    assert stem([21, 12, 11]) == "Coin_Face1_Trace 2_Pocket"


def test_one_of_several_setups_is_named_so_it_cannot_replace_the_whole_job():
    assert stem([1]) == "Coin_Setup1"


def test_setup_and_toolpath_mix():
    assert stem([1, 21]) == "Coin_Setup1_Pocket"


def test_nothing_ticked_is_just_the_base():
    assert stem([]) == "Coin"


def test_same_selection_gives_the_same_file():
    assert client.machine_name(stem([11, 12]), "cnc") == client.machine_name(stem([12, 11]), "cnc")


def test_long_selections_stop_at_the_api_limit():
    long_names = {i: f"Toolpath number {i} with a long name" for i in range(1, 10)}
    tree = [(1, list(range(2, 10)))]
    name = client.machine_name(selection.file_stem("Coin", tree, set(range(2, 9)), long_names), "cnc")
    assert len(name) == 100
    assert name.startswith("Coin_Toolpath_number_2_")
    assert name.endswith(".cnc")
    assert validate_name(name) is None


def test_trimming_never_leaves_a_separator_before_the_extension():
    name = client.safe_name("a" * 95 + "_b.cnc")
    assert name == "a" * 95 + ".cnc"
