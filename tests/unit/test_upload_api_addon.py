"""The Upload API's top-bar item: added after the tool item while the API runs, removed after."""

from types import SimpleNamespace

from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label

from carveracontroller.addons.upload_api import addon as addon_module
from carveracontroller.addons.upload_api.addon import UploadApiAddon


class FakeTopDataView(Button):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.data_icon = self.main_text = self.minr_text = self.tooltip_txt = ""
        self.active = False


def make_addon(snapshot, monkeypatch):
    # The real TopDataView needs the app's kv rules; this test is about where the item goes.
    monkeypatch.setattr(addon_module, "Factory", SimpleNamespace(TopDataView=FakeTopDataView))
    bar = BoxLayout()
    tool, after = Label(text="tool"), Label(text="spacer")
    bar.add_widget(Label(text="feed"))
    bar.add_widget(tool)
    bar.add_widget(after)
    addon = UploadApiAddon.__new__(UploadApiAddon)  # skip settings and start; test the view only
    addon.makera = SimpleNamespace(ids={"tool_data_view": tool})
    addon.api = SimpleNamespace(snapshot=lambda: snapshot[0])
    addon.view = None
    return addon, bar


def visible_order(bar):
    return [getattr(w, "text", "") or "upload" for w in reversed(bar.children)]


def test_item_goes_after_the_tool_item_while_running_and_away_after(monkeypatch):
    running = {
        "running": True,
        "address": ("127.0.0.1", 8642),
        "receiving": ["coin.nc"],
        "uploading": None,
        "queued": 0,
        "waiting": "",
        "last": None,
    }
    snapshot = [running]
    addon, bar = make_addon(snapshot, monkeypatch)
    addon.refresh()
    assert visible_order(bar) == ["feed", "tool", "upload", "spacer"]
    assert (addon.view.main_text, addon.view.minr_text, addon.view.active) == ("Receiving", "coin.nc", True)
    addon.refresh()  # updating doesn't add it twice
    assert visible_order(bar) == ["feed", "tool", "upload", "spacer"]
    snapshot[0] = {"running": False}
    addon.refresh()
    assert visible_order(bar) == ["feed", "tool", "spacer"]
