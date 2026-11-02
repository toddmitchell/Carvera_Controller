"""The Upload API inside the Controller: its settings panel, starting and stopping it, the top-bar
item and the uploads popup. Kept here so the Controller's own code only needs a few hooks:

- ``Makera.__init__``: ``self.upload_api = UploadApiAddon(self)`` (after the other settings panels)
- controller settings changed: ``self.upload_api.settings_changed(keys)``
- app stop: ``upload_api.stop()``
- ``Makera.uploadLocalFile(..., remote_dir=, on_finished=)`` for the uploads themselves
"""

from __future__ import annotations

import json
import logging
import os
import time

from kivy.app import App
from kivy.clock import Clock
from kivy.config import Config
from kivy.factory import Factory

from carveracontroller.translation import tr

from .api import DEFAULT_PORT, UploadApi, summarize
from .bridge import MakeraBridge
from .popup import UploadApiPopup

logger = logging.getLogger(__name__)

SECTION = "carvera"
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "upload_api_config.json")


class UploadApiAddon:
    def __init__(self, makera):
        self.makera = makera
        self.bridge = MakeraBridge(makera)
        self.api = UploadApi(self.bridge, on_change=self._changed)
        self.view = None  # the top-bar item, while the API runs
        self._add_settings_panel()
        self.configure()

    # -- settings -------------------------------------------------------------

    def _add_settings_panel(self) -> None:
        with open(SETTINGS_FILE) as fh:
            definition = json.load(fh)
        for setting in definition:
            if "default" in setting:
                Config.setdefault(setting["section"], setting["key"], setting.pop("default"))
        self.makera.config_popup.settings_panel.add_json_panel(tr._("Upload API"), Config, data=json.dumps(definition))

    def settings_changed(self, keys) -> None:
        if any(k.startswith("upload_api_") for k in keys):
            self.configure()

    def configure(self) -> None:
        """Start, restart or stop the API from the settings (every key has its default)."""
        if not Config.getboolean(SECTION, "upload_api_enabled"):
            self.api.stop()
            return
        lan = Config.getboolean(SECTION, "upload_api_lan")
        token = Config.get(SECTION, "upload_api_token").strip()
        origins = Config.get(SECTION, "upload_api_origins").replace(",", " ").split()
        try:
            port = int(float(Config.get(SECTION, "upload_api_port")))
        except ValueError:
            port = DEFAULT_PORT
        try:
            self.api.start("0.0.0.0" if lan else "127.0.0.1", port, token=token, allowed_origins=origins)
            self.bridge.notify("Upload API listening on %s:%d" % self.api.address)
        except Exception as exc:
            logger.exception("Upload API failed to start")
            self.bridge.notify("Upload API not started: %s" % exc)

    def stop(self) -> None:
        self.api.stop()

    # -- top bar ----------------------------------------------------------------

    def _changed(self) -> None:
        # Called from the API's worker and HTTP threads; the UI is updated on the main thread.
        Clock.schedule_once(self.refresh, 0)

    def refresh(self, *_args) -> None:
        """Show, update or remove the top-bar item from the API's current state."""
        state = summarize(self.api.snapshot(), time.time())
        if state["refresh_in"]:  # "Uploaded"/"Failed" fades back
            Clock.schedule_once(self.refresh, state["refresh_in"])
        if not state["visible"]:
            if self.view is not None and self.view.parent is not None:
                self.view.parent.remove_widget(self.view)
            return
        view = self._top_bar_item()
        if view is None:
            return
        view.main_text = state["main"]
        view.minr_text = state["minor"]
        view.active = state["active"]
        view.tooltip_txt = state["tooltip"]

    def _top_bar_item(self):
        """The item, placed after the tool item in the top bar (created the first time)."""
        tool_view = self.makera.ids.get("tool_data_view")
        if tool_view is None or tool_view.parent is None:
            return None
        if self.view is None:
            self.view = Factory.TopDataView()
            self.view.data_icon = "data/upload.png"
            # The shared style's main line is sized for short numbers ("1000"); words need a smaller font.
            for label in self.view.walk(restrict=True):
                if type(label).__name__ == "ScrollingLabel":  # its sizes are strings, e.g. "18sp"
                    label.font_size = label.effective_font_size = "13sp"
            self.view.bind(on_release=lambda *_: self.show_details())
        if self.view.parent is None:
            bar = tool_view.parent
            # Kivy keeps children last-first, so this index puts the item just after the tool item.
            bar.add_widget(self.view, index=bar.children.index(tool_view))
        return self.view

    # -- uploads popup ------------------------------------------------------------

    def show_details(self) -> None:
        """List the recent API uploads; clicking a finished one loads it (never runs it)."""
        if self.api.running:
            UploadApiPopup(self.api.address, self.api.queue.recent()[:30], self.load_uploaded_file).open()

    def load_uploaded_file(self, remote_path: str) -> None:
        """Load a file the API put on the machine, as picking it in the file browser does:
        preview and select it. The operator still starts the job."""
        m = self.makera
        if App.get_running_app().playing:
            m.show_message_popup(tr._("A job is running. Load the file after it finishes."), False)
            return
        # Where the Controller keeps its local copy of a machine file (see Makera.doUpload).
        local_cached = os.path.join(m.temp_dir, remote_path.replace("/sd/", "").replace("\\sd\\", ""))
        if not os.path.exists(local_cached):
            m.show_message_popup(
                tr._("The local copy is gone. Open the file from the machine's file browser:") + "\n" + remote_path,
                False,
            )
            return
        m.select_file(remote_path, local_cached)
