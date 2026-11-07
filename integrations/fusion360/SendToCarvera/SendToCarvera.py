"""Fusion add-in: a Send to Carvera button in the Manufacture workspace.

It posts an NC Program (with that program's own post and settings) into a temporary folder and hands
the file to the Carvera Controller's Upload API. A file with that name on the machine is replaced.
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

COMMAND_ID = "carveraUploadApiSendToCarvera"
WORKSPACE_ID = "CAMEnvironment"
PANEL_ID = "CAMActionPanel"
FALLBACK_PANEL_ID = "carveraUploadApiPanel"
RESOURCES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")

_app = adsk.core.Application.get()
_ui = _app.userInterface
_handlers = []  # Fusion only holds weak references to event handlers.


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


def _programs(cam) -> list:
    return [cam.ncPrograms.item(i) for i in range(cam.ncPrograms.count)]


def _parameter(program, name: str):
    return program.parameters.itemByName(name)


def _post_name(program) -> str:
    try:
        return program.postConfiguration.description
    except Exception:  # No post chosen yet.
        return ""


def _machine_file(program) -> str:
    """The name the file will have on the machine."""
    extension = program.postConfiguration.extension.lstrip(".")
    return api.safe_name(f"{_parameter(program, 'nc_program_filename').value.value}.{extension}")


def _details(program) -> str:
    post = _post_name(program)
    if not post:
        return "<b>This NC Program has no post.</b> Edit it and choose the Carvera post."
    lines = [f"<b>Post:</b> {html.escape(post)}"]
    if not any(word in post.lower() for word in ("carvera", "makera")):
        lines.append("<b>This doesn't look like a Carvera post.</b> Check it before sending.")
    try:
        lines.append(f"<b>File:</b> {html.escape(_machine_file(program))}")
        lines.append("A file with that name on the machine is replaced.")
    except api.ApiError as exc:
        lines.append(html.escape(str(exc)))
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


def _send(program, settings: dict) -> None:
    api.get_status(settings)  # Say so now if the Controller isn't there, before posting.
    folder = tempfile.mkdtemp(prefix="carvera_post_")
    try:
        files = _post(program, folder)
        # Check every name before queueing anything, so a job is never half sent.
        names = [api.safe_name(f) for f in files]
        contents = []
        for file in files:
            with open(os.path.join(folder, file), "rb") as fh:
                contents.append(fh.read())
        jobs = [api.queue_upload(settings, name, data) for name, data in zip(names, contents)]
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    _ui.messageBox(
        api.summarize(_watch(settings, jobs)) + "\n\nNothing was started. Start the job on the machine.",
        "Send to Carvera",
    )


def _selected_program(inputs, programs):
    item = adsk.core.DropDownCommandInput.cast(inputs.itemById("program")).selectedItem
    return programs[item.index] if item else None


class _Execute(adsk.core.CommandEventHandler):
    def __init__(self, programs):
        super().__init__()
        self.programs = programs

    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            settings = {"port": inputs.itemById("port").value, "token": inputs.itemById("token").value.strip()}
            api.save_settings(settings)
            _send(_selected_program(inputs, self.programs), settings)
        except api.ApiError as exc:
            _ui.messageBox(str(exc), "Send to Carvera")
        except Exception:
            _report_failure()


class _InputChanged(adsk.core.InputChangedEventHandler):
    def __init__(self, programs):
        super().__init__()
        self.programs = programs

    def notify(self, args):
        try:
            if args.input.id == "program":
                inputs = args.inputs
                inputs.itemById("details").formattedText = _details(_selected_program(inputs, self.programs))
        except Exception:
            _report_failure()


class _CommandCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cam = _active_cam()
            programs = _programs(cam) if cam else []
            if not programs:
                _ui.messageBox(
                    "This document has no NC Program yet. In the Manufacture workspace, choose Actions → NC Program, "
                    "pick your toolpaths and the Carvera post, then click Send to Carvera.",
                    "Send to Carvera",
                )
                return  # With no inputs and no execute handler, the command just ends.

            command = args.command
            command.okButtonText = "Send"
            command.isExecutedWhenPreEmpted = False
            inputs = command.commandInputs
            settings = api.load_settings()

            dropdown = inputs.addDropDownCommandInput(
                "program", "NC Program", adsk.core.DropDownStyles.TextListDropDownStyle
            )
            chosen = next((i for i, p in enumerate(programs) if p.isSelected), 0)
            for i, program in enumerate(programs):
                dropdown.listItems.add(program.name, i == chosen)
            inputs.addTextBoxCommandInput("details", "", _details(programs[chosen]), 4, True)
            try:
                status = api.describe_status(api.get_status(settings))
            except api.ApiError as exc:
                status = str(exc)
            inputs.addTextBoxCommandInput("status", "Controller", status, 3, True)

            group = inputs.addGroupCommandInput("connection", "Connection")
            group.isExpanded = False
            group.children.addIntegerSpinnerCommandInput("port", "Port", 1, 65535, 1, settings["port"])
            token = group.children.addStringValueInput("token", "Token (if set)", settings["token"])
            token.isPassword = True

            for event, handler in (
                (command.execute, _Execute(programs)),
                (command.inputChanged, _InputChanged(programs)),
            ):
                event.add(handler)
                _handlers.append(handler)
        except Exception:
            _report_failure()


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

        panels = _ui.workspaces.itemById(WORKSPACE_ID).toolbarPanels
        panel = panels.itemById(PANEL_ID) or panels.add(FALLBACK_PANEL_ID, "Carvera")
        control = panel.controls.addCommand(definition)
        control.isPromoted = True
    except Exception:
        _report_failure()


def stop(context):
    try:
        panels = _ui.workspaces.itemById(WORKSPACE_ID).toolbarPanels
        for panel_id in (PANEL_ID, FALLBACK_PANEL_ID):
            panel = panels.itemById(panel_id)
            control = panel.controls.itemById(COMMAND_ID) if panel else None
            if control:
                control.deleteMe()
        fallback = panels.itemById(FALLBACK_PANEL_ID)
        if fallback:
            fallback.deleteMe()
        definition = _ui.commandDefinitions.itemById(COMMAND_ID)
        if definition:
            definition.deleteMe()
        _handlers.clear()
    except Exception:
        _report_failure()
