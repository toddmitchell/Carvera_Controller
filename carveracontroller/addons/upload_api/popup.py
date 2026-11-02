"""The Upload API popup: where the API listens and the recent uploads. A finished upload can be
loaded (previewed and selected, as from the file browser). Loading never starts a job."""

from __future__ import annotations

import time
from typing import Callable

from kivy.app import App
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView

from carveracontroller.translation import tr

STATE_TEXT = {
    "queued": tr._("waiting"),
    "uploading": tr._("sending"),
    "done": tr._("on the machine"),
    "failed": tr._("failed"),
    "cancelled": tr._("cancelled"),
}


class UploadApiPopup(Popup):
    def __init__(self, address: tuple, jobs: list, on_load: Callable[[str], None], **kwargs):
        super().__init__(**kwargs)
        self.title = tr._("Upload API")
        self.size_hint = (0.7, 0.75)
        self.auto_dismiss = True
        self._on_load = on_load

        theme = getattr(App.get_running_app(), "theme", None)
        muted = tuple(getattr(theme, "text_muted", (0.7, 0.7, 0.7, 1)))

        root = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        host, port = address
        root.add_widget(
            Label(
                text=tr._("Listening on")
                + f" {host}:{port}. "
                + tr._("Click a file to load it; jobs never start by themselves."),
                size_hint_y=None,
                height=dp(40),
                color=muted,
                halign="left",
                valign="middle",
                text_size=(None, None),
            )
        )

        rows = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4))
        rows.bind(minimum_height=rows.setter("height"))
        if not jobs:
            rows.add_widget(Label(text=tr._("No uploads yet."), size_hint_y=None, height=dp(40), color=muted))
        for job in jobs:
            when = time.strftime("%H:%M", time.localtime(job.created_at))
            status = STATE_TEXT.get(job.state, job.state)
            if job.state == "failed" and job.message:
                status += f": {job.message}"
            loadable = job.state == "done" and bool(job.remote_path)
            btn = Button(
                text=f"{when}   {job.name}   [{status}]",
                size_hint_y=None,
                height=dp(44),
                halign="left",
                valign="middle",
                disabled=not loadable,
            )
            btn.bind(size=lambda b, *_: setattr(b, "text_size", (b.width - dp(20), None)))
            if loadable:
                btn.bind(on_release=lambda _b, path=job.remote_path: self._load(path))
            rows.add_widget(btn)

        scroll = ScrollView(do_scroll_x=False)
        scroll.add_widget(rows)
        root.add_widget(scroll)

        close = Button(text=tr._("Close"), size_hint_y=None, height=dp(44))
        close.bind(on_release=lambda *_: self.dismiss())
        root.add_widget(close)
        self.content = root

    def _load(self, remote_path: str) -> None:
        self.dismiss()
        self._on_load(remote_path)
