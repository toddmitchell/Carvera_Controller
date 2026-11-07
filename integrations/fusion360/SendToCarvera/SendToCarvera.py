"""Fusion add-in: a Send to Carvera button in the Manufacture workspace.

It's also on the right-click menu. It posts the setups and toolpaths you tick, or an NC Program, into a
temporary folder and hands the file to the Carvera Controller's Upload API. A file with that name on the machine is replaced.
It uploads only: the operator starts the job on the machine.
"""

from __future__ import annotations

import html
import os
import shutil
import tempfile
import time
import traceback

import adsk.cam
import adsk.core

from . import carvera_api as api
from . import selection

COMMAND_ID = "carveraUploadApiSendToCarvera"
WORKSPACE_ID = "CAMEnvironment"
TAB_ID = "MillingTab"
PANEL_ID = "carveraUploadApiPanel"
MENU_SEPARATOR_ID = "carveraUploadApiMenuSeparator"
TOOLPATHS_LABEL = "Toolpaths ticked below"
RESOURCES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")

_app = adsk.core.Application.get()
_ui = _app.userInterface
NO_POST = "No Carvera post found in your post library. Add it under Manage → Post Library, then try again."
_right_click = None
_handlers = []  # Fusion only holds weak references to event handlers.


def _text(value: str) -> str:
    """Plain text for a dialog's text box. Quotes stay as they are: Fusion shows `&#x27;` literally."""
    return html.escape(value, quote=False)


def _report_failure() -> None:
    _ui.messageBox(f"Send to Carvera failed:\n{traceback.format_exc()}")


def _active_cam():
    document = _app.activeDocument
    if not document:
        return None
    try:
        product = document.products.itemByProductType("CAMProductType")
    except Exception:  # Fusion raises for a document with no Manufacture data.
        return None
    return adsk.cam.CAM.cast(product) if product else None


class _Choice:
    """What to send: an NC Program in the document, or operations that need a temporary one."""

    def __init__(self, program=None, operations=None):
        self.program = program
        self.operations = operations


def _items(collection) -> list:
    return [collection.item(i) for i in range(collection.count)]


def _folder_selection(folders) -> list:
    """Ids from folders selected in the browser. Selecting a folder doesn't select what's inside it."""
    ids = []
    for folder in _items(folders):
        if folder.isSelected:
            ids.extend(op.operationId for op in _items(folder.allOperations))
        else:
            ids.extend(_folder_selection(folder.folders))
    return ids


def _browser_selection(cam) -> list:
    """Ids of what's selected in Fusion's browser, which is what a right-click acts on."""
    ids = []
    for setup in _items(cam.setups):
        if setup.isSelected:
            ids.append(setup.operationId)
            continue
        ids.extend(op.operationId for op in _items(setup.allOperations) if op.isSelected)
        ids.extend(_folder_selection(setup.folders))
    return ids


def _carvera_posts() -> list:
    """Carvera posts in the post libraries, your own first, one per description."""
    library = adsk.cam.CAMManager.get().libraryManager.postLibrary
    posts = {}
    for location in (
        adsk.cam.LibraryLocations.LocalLibraryLocation,
        adsk.cam.LibraryLocations.CloudLibraryLocation,
        adsk.cam.LibraryLocations.Fusion360LibraryLocation,
    ):
        try:
            query = library.createQuery(location)
            query.capability = adsk.cam.PostCapabilities.Milling
            found = query.execute()
        except RuntimeError:  # e.g. the cloud library while offline
            continue
        for post in found:
            if _is_carvera_post(post.description):
                posts.setdefault(post.description, post)
    return list(posts.values())


def _is_carvera_post(description: str) -> bool:
    return any(word in description.lower() for word in ("carvera", "makera"))


def _document_key() -> str:
    try:
        return _app.activeDocument.dataFile.id
    except (RuntimeError, AttributeError):  # Never saved, so there's no data file.
        return ""


def _default_name(remembered: dict) -> str:
    """The name used last for this document, or the document's name without its version."""
    if remembered["name"]:
        return remembered["name"]
    try:
        return _app.activeDocument.dataFile.name
    except (RuntimeError, AttributeError):
        return _app.activeDocument.name


def _parameter(program, name: str):
    return program.parameters.itemByName(name)


def _post_name(program) -> str:
    try:
        return program.postConfiguration.description
    except Exception:  # No post chosen yet.
        return ""


def _machine_file(program) -> str:
    """The name the file will have on the machine."""
    return api.machine_name(_parameter(program, "nc_program_filename").value.value, program.postConfiguration.extension)


def _details(program) -> str:
    post = _post_name(program)
    if not post:
        return "<b>This NC Program has no post.</b> Edit it and choose the Carvera post."
    lines = [f"<b>Post:</b> {_text(post)}"]
    if not _is_carvera_post(post):
        lines.append("<b>This doesn't look like a Carvera post.</b> Check it before sending.")
    try:
        lines.append(f"<b>File:</b> {_text(_machine_file(program))}")
        lines.append("A file with that name on the machine is replaced.")
    except api.ApiError as exc:
        lines.append(_text(str(exc)))
    return "<br>".join(lines)


def _post(program, folder: str) -> list:
    """Post into `folder` and return the G-code files written, with the program's settings put back after."""
    # Only for this post. Otherwise a post could open an editor or publish to the cloud on every send.
    overrides = {
        "nc_program_output_folder": folder.replace("\\", "/"),
        "nc_program_openInEditor": False,
        "nc_program_postToFusionTeam": False,
    }
    saved = {}
    try:
        for name, value in overrides.items():
            parameter = _parameter(program, name)
            if parameter:
                saved[name] = parameter.value.value
                parameter.value.value = value
        options = adsk.cam.NCProgramPostProcessOptions.create()
        # The default skips invalid operations silently, which would send a job with cuts missing.
        options.postProcessExecutionBehavior = adsk.cam.PostProcessExecutionBehaviors.PostProcessExecutionBehavior_Fail
        try:
            posted = program.postProcess(options)
        except RuntimeError as exc:  # Out-of-date or invalid toolpaths land here, with Fusion's reason.
            raise api.ApiError(f"Fusion couldn't post this NC Program. Nothing was sent.\n\n{exc}") from exc
        if not posted:
            raise api.ApiError("Fusion couldn't post this NC Program. Nothing was sent.")
    finally:
        for name, value in saved.items():
            _parameter(program, name).value.value = value
    written = sorted(f for f in os.listdir(folder) if os.path.splitext(f)[1].lower() in api.GCODE_EXTENSIONS)
    if not written:
        raise api.ApiError("The post didn't write a G-code file. Check that every toolpath is generated.")
    return written


def _watch(settings: dict, jobs: list) -> list:
    """Show progress until `keep_watching` says stop. Cancelling only stops the watching."""
    dialog = _ui.createProgressDialog()
    dialog.isCancelButtonShown = True
    dialog.show("Send to Carvera", "Sending to the Carvera Controller…", 0, 1, 0)
    started = time.monotonic()
    try:
        while api.keep_watching(jobs, time.monotonic() - started) and not dialog.wasCancelled:
            time.sleep(0.25)
            adsk.doEvents()
            jobs = [api.get_job(settings, job["id"]) for job in jobs]
    except api.ApiError:
        pass  # The queued jobs are the Controller's now; report what we last knew.
    finally:
        dialog.hide()
    return jobs


def _temporary_program(cam, operations: list, stem: str, post):
    ncinput = cam.ncPrograms.createInput()
    ncinput.displayName = "Send to Carvera (temporary)"
    ncinput.parameters.itemByName("nc_program_filename").value.value = stem
    ncinput.operations = operations
    program = cam.ncPrograms.add(ncinput)
    program.postConfiguration = post
    return program


def _send(cam, choice: _Choice, settings: dict, name: str, post) -> None:
    api.get_status(settings)  # Say so now if the Controller isn't there, before posting.
    folder = tempfile.mkdtemp(prefix="carvera_post_")
    try:
        if choice.program:
            files = _post(choice.program, folder)
        else:
            if not post:
                raise api.ApiError(NO_POST)
            stem = os.path.splitext(api.machine_name(name, post.extension))[0]
            program = _temporary_program(cam, choice.operations, stem, post)
            try:
                files = _post(program, folder)
            finally:
                program.deleteMe()
        # Check every name before queueing anything, so a job is never half sent.
        names = [api.safe_name(f) for f in files]
        contents = []
        for file in files:
            with open(os.path.join(folder, file), "rb") as fh:
                contents.append(fh.read())
        jobs = [api.queue_upload(settings, n, data) for n, data in zip(names, contents)]
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    _ui.messageBox(
        api.summarize(_watch(settings, jobs)) + "\n\nNothing was started. Start the job on the machine.",
        "Send to Carvera",
    )


def _dropdown(inputs, input_id: str):
    return adsk.core.DropDownCommandInput.cast(inputs.itemById(input_id))


class _Dialog:
    """The dialog's inputs for a document's programs, setups and toolpaths."""

    def __init__(self, cam, programs, posts, inputs, base_name: str):
        self.cam = cam
        self.programs = programs
        self.posts = posts
        self.inputs = inputs
        self.base_name = base_name
        self.suggested_name = ""
        self.name_edited = False  # Typed by hand in this dialog, so ticking boxes leaves it alone.
        self.objects = {}  # operation id -> setup or operation
        self.boxes = {}  # operation id -> checkbox
        self.groups = []
        self.tree = []  # (setup id, [operation ids])

    def add_setups(self, ticked_from) -> None:
        for setup in _items(self.cam.setups):
            operations = [op for op in _items(setup.allOperations) if not op.isSuppressed]
            self.objects[setup.operationId] = setup
            self.objects.update((op.operationId, op) for op in operations)
            self.tree.append((setup.operationId, [op.operationId for op in operations]))
        ticked = ticked_from(self.tree)
        for setup in _items(self.cam.setups):
            setup_id = setup.operationId
            operations = next(ops for s, ops in self.tree if s == setup_id)
            group = self.inputs.addGroupCommandInput(f"setup_{setup_id}", setup.name)
            group.isExpanded = len(self.tree) == 1 or (bool(ticked) and any(i in ticked for i in operations))
            self.groups.append(group)
            self._box(group, setup_id, "Whole setup", ticked)
            for op_id in operations:
                self._box(group, op_id, _operation_label(self.objects[op_id]), ticked)

    def _box(self, group, item_id: int, label: str, ticked) -> None:
        self.boxes[item_id] = group.children.addBoolValueInput(f"tick_{item_id}", label, True, "", item_id in ticked)

    def ticked(self) -> set:
        return {item for item, box in self.boxes.items() if box.value}

    def clicked(self, changed_id: str) -> None:
        """Keep a setup's box and its toolpaths' boxes in step after a click."""
        clicked = int(changed_id.removeprefix("tick_"))
        for setup, operations in self.tree:
            if clicked == setup:
                for op in operations:
                    self.boxes[op].value = self.boxes[setup].value
            elif clicked in operations:
                self.boxes[setup].value = all(self.boxes[op].value for op in operations)

    def sends_everything(self) -> bool:
        return selection.sends_everything(self.tree, self.ticked())

    def _suggestion(self) -> str:
        names = {item: obj.name for item, obj in self.objects.items()}
        return selection.file_stem(self.base_name, self.tree, self.ticked(), names)

    def suggest_name(self) -> None:
        """Put the suggested name in the field, unless one has been typed."""
        if not self.name_edited:
            self.suggested_name = self._suggestion()
            self.inputs.itemById("name").value = self.suggested_name

    def name_changed(self) -> None:
        # Compared with the suggestion because Fusion may also report changes made by code. Clearing the
        # field goes back to suggesting.
        value = self.inputs.itemById("name").value.strip()
        self.name_edited = bool(value) and value != self.suggested_name

    def file_name(self) -> str:
        return self.inputs.itemById("name").value.strip() or self._suggestion()

    def program(self):
        """The chosen NC Program, or None for toolpaths. The first entry is always toolpaths."""
        index = _dropdown(self.inputs, "source").selectedItem.index
        return self.programs[index - 1] if index else None

    def post(self):
        item = _dropdown(self.inputs, "post").selectedItem
        return self.posts[item.index] if item and self.posts else None

    def choice(self) -> _Choice:
        program = self.program()
        if program:
            return _Choice(program=program)
        return _Choice(operations=[self.objects[i] for i in selection.to_send(self.tree, self.ticked())])

    def is_ready(self) -> bool:
        return bool(self.program()) or bool(self.post() and self.ticked())

    def refresh(self) -> None:
        program = self.program()
        self.inputs.itemById("post").isVisible = not program and bool(self.posts)
        self.inputs.itemById("name").isVisible = not program
        for group in self.groups:
            group.isVisible = not program
        self.inputs.itemById("details").formattedText = _details(program) if program else self._toolpaths()

    def _toolpaths(self) -> str:
        post = self.post()
        if not post:
            return _text(NO_POST)
        if not self.ticked():
            return "Tick the setups or toolpaths to send."
        try:
            file = _text(api.machine_name(self.file_name(), post.extension))
        except api.ApiError as exc:
            return _text(str(exc))
        return f"<b>File:</b> {file}<br>A file with that name on the machine is replaced."


def _operation_label(operation) -> str:
    return operation.name if operation.hasToolpath else f"{operation.name} (no toolpath)"


class _Execute(adsk.core.CommandEventHandler):
    def __init__(self, dialog: _Dialog, settings: dict):
        super().__init__()
        self.dialog = dialog
        self.settings = settings

    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            connection = adsk.core.GroupCommandInput.cast(inputs.itemById("connection")).children
            dialog = self.dialog
            name = dialog.file_name()
            post = dialog.post()
            program = dialog.program()
            settings = {
                **self.settings,
                "port": connection.itemById("port").value,
                "token": connection.itemById("token").value.strip(),
                "post": post.description if post else self.settings["post"],
            }
            remembered = api.remembered(settings, _document_key())
            documents = dict(settings["documents"])
            documents[_document_key()] = {
                # Only a full send sets the plain name. Other names say what they hold.
                "name": name if not program and dialog.sends_everything() else remembered["name"],
                "source": program.operationId if program else None,
                # An NC Program send leaves the remembered ticks alone.
                "operations": remembered["operations"] if program else sorted(dialog.ticked()),
            }
            settings["documents"] = documents
            api.save_settings(settings)
            _send(dialog.cam, dialog.choice(), settings, name, post)
        except api.ApiError as exc:
            _ui.messageBox(str(exc), "Send to Carvera")
        except Exception:
            _report_failure()


class _InputChanged(adsk.core.InputChangedEventHandler):
    def __init__(self, dialog: _Dialog):
        super().__init__()
        self.dialog = dialog

    def notify(self, args):
        try:
            if args.input.id.startswith("tick_"):
                self.dialog.clicked(args.input.id)
                self.dialog.suggest_name()
            elif args.input.id == "name":
                self.dialog.name_changed()
            self.dialog.refresh()
        except Exception:
            _report_failure()


class _Validate(adsk.core.ValidateInputsEventHandler):
    def __init__(self, dialog: _Dialog):
        super().__init__()
        self.dialog = dialog

    def notify(self, args):
        try:
            args.areInputsValid = self.dialog.is_ready()
        except Exception:
            _report_failure()


def _starting_source(programs: list, browser: list, remembered: dict) -> int:
    """The Send entry to start on: an NC Program selected in the browser, or toolpaths selected there,
    or whatever was sent last for this document. Entry 0 is toolpaths; programs follow it."""
    for i, program in enumerate(programs):
        if program.isSelected:
            return i + 1
    if browser:
        return 0
    return next((i + 1 for i, p in enumerate(programs) if p.operationId == remembered["source"]), 0)


class _CommandCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cam = _active_cam()
            programs = _items(cam.ncPrograms) if cam else []
            if not programs and not (cam and cam.setups.count):
                _ui.messageBox(
                    "This document has no toolpaths to send. Create them in the Manufacture workspace.",
                    "Send to Carvera",
                )
                return  # With no inputs and no execute handler, the command just ends.

            command = args.command
            command.okButtonText = "Send"
            command.isExecutedWhenPreEmpted = False
            inputs = command.commandInputs
            settings = api.load_settings()
            remembered = api.remembered(settings, _document_key())
            posts = _carvera_posts()
            browser = _browser_selection(cam)
            dialog = _Dialog(cam, programs, posts, inputs, _default_name(remembered))

            source = inputs.addDropDownCommandInput("source", "Send", adsk.core.DropDownStyles.TextListDropDownStyle)
            start = _starting_source(programs, browser, remembered)
            source.listItems.add(TOOLPATHS_LABEL, start == 0)
            for i, program in enumerate(programs):
                source.listItems.add(program.name, start == i + 1)
            post_input = inputs.addDropDownCommandInput("post", "Post", adsk.core.DropDownStyles.TextListDropDownStyle)
            last_post = next((i for i, p in enumerate(posts) if p.description == settings["post"]), 0)
            for i, post in enumerate(posts):
                post_input.listItems.add(post.description, i == last_post)
            inputs.addStringValueInput("name", "File name", "")
            dialog.add_setups(lambda tree: selection.initial(tree, browser, remembered["operations"]))
            inputs.addTextBoxCommandInput("details", "", "", 4, True)
            dialog.suggest_name()
            dialog.refresh()
            try:
                status = api.describe_status(api.get_status(settings))
            except api.ApiError as exc:
                status = str(exc)
            inputs.addTextBoxCommandInput("status", "Controller", _text(status), 3, True)

            group = inputs.addGroupCommandInput("connection", "Connection")
            group.isExpanded = False
            group.children.addIntegerSpinnerCommandInput("port", "Port", 1, 65535, 1, settings["port"])
            token = group.children.addStringValueInput("token", "Token (if set)", settings["token"])
            token.isPassword = True

            for event, handler in (
                (command.execute, _Execute(dialog, settings)),
                (command.inputChanged, _InputChanged(dialog)),
                (command.validateInputs, _Validate(dialog)),
            ):
                event.add(handler)
                _handlers.append(handler)
        except Exception:
            _report_failure()


class _RightClick(adsk.core.MarkingMenuEventHandler):
    """Adds the command to the right-click menu in Manufacture. Fusion rebuilds the menu each time."""

    def notify(self, args):
        try:
            if _ui.activeWorkspace.id != WORKSPACE_ID:
                return
            controls = args.linearMarkingMenu.controls
            if not controls.itemById(COMMAND_ID):
                controls.addSeparator(MENU_SEPARATOR_ID)
                controls.addCommand(_ui.commandDefinitions.itemById(COMMAND_ID))
        except Exception:  # A dialog on every right-click would make Fusion unusable.
            _app.log(f"Send to Carvera, right-click menu:\n{traceback.format_exc()}")


def run(context):
    try:
        definition = _ui.commandDefinitions.itemById(COMMAND_ID) or _ui.commandDefinitions.addButtonDefinition(
            COMMAND_ID,
            "Send to Carvera",
            "Post an NC Program and upload it through the Carvera Controller, replacing the file of that name. "
            "It never starts a job.",
            RESOURCES,
        )
        handler = _CommandCreated()
        definition.commandCreated.add(handler)
        _handlers.append(handler)

        # Its own panel on the Milling tab, so the button is on the toolbar rather than inside a menu.
        workspace = _ui.workspaces.itemById(WORKSPACE_ID)
        tab = workspace.toolbarTabs.itemById(TAB_ID)
        panels = tab.toolbarPanels if tab else workspace.toolbarPanels
        panel = panels.itemById(PANEL_ID) or panels.add(PANEL_ID, "CARVERA")
        control = panel.controls.itemById(COMMAND_ID) or panel.controls.addCommand(definition)
        control.isPromotedByDefault = True
        control.isPromoted = True

        global _right_click
        _right_click = _RightClick()
        _ui.markingMenuDisplaying.add(_right_click)
    except Exception:
        _report_failure()


def stop(context):
    try:
        if _right_click:
            _ui.markingMenuDisplaying.remove(_right_click)
        workspace = _ui.workspaces.itemById(WORKSPACE_ID)
        tab = workspace.toolbarTabs.itemById(TAB_ID)
        panels = tab.toolbarPanels if tab else workspace.toolbarPanels
        panel = panels.itemById(PANEL_ID)
        if panel:
            control = panel.controls.itemById(COMMAND_ID)
            if control:
                control.deleteMe()
            panel.deleteMe()
        definition = _ui.commandDefinitions.itemById(COMMAND_ID)
        if definition:
            definition.deleteMe()
        _handlers.clear()
    except Exception:
        _report_failure()
