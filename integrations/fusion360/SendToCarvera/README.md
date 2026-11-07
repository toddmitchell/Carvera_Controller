# Send to Carvera (Fusion add-in)

A button in Fusion's Manufacture workspace. It posts an NC Program and hands the file to the Carvera
Controller's [Upload API](../../../carveracontroller/addons/upload_api/README.md). The Controller
uploads it to `/sd/gcodes` over the connection it already has, as soon as the machine is Idle.

**It only uploads.** You start the job on the machine.

## Install

1. In the Controller, turn on Settings → **Upload API**.
2. In Fusion, open Utilities → **Add-Ins** → **Scripts and Add-Ins**, click **+** next to Add-Ins, and
   choose this folder (`SendToCarvera`).
3. Click **Run**. It loads by itself each time Fusion starts.

The button appears in Manufacture → Milling → **Actions**, next to the other Actions commands.

## Use

1. Make an NC Program once: Actions → **NC Program**, with your toolpaths and the Carvera post.
2. Click **Send to Carvera**, check the post and file name, and click **Send**.

If you've selected an NC Program in the browser, it's chosen for you. Send it again after changing
toolpaths and the file on the machine is replaced.

- **File name:** the NC Program's *Name* setting. Characters the API doesn't allow become `_`, and the
  dialog shows the result. Change that setting to keep a separate file.
- **Post:** the NC Program's own post, properties and tool order. The dialog warns if the post's name
  doesn't mention Carvera or Makera.
- **Toolpaths with problems:** posting stops and nothing is sent. Fusion's default would leave those
  operations out without saying so, and the job would be missing cuts.
- **Your NC Program:** the file goes to a temporary folder, not your output folder. The add-in turns
  off "open in editor" and cloud posting for that one post, then puts all three settings back. Fusion
  may mark the document as changed.
- **Machine busy:** the file stays queued in the Controller, and the dialog closes after about 10
  seconds. The Controller finishes the upload by itself, and its top bar shows it.
- **A file already loaded:** if the Controller has the replaced file open, load it again before running.

Connection settings are under **Connection** in the dialog. The port and any token are saved in
`~/.carvera_fusion_addin.json`, readable only by you. The add-in only talks to the Controller on this
computer.

## Code

- `SendToCarvera.py`: the Fusion side (button, dialog, posting). Runs only inside Fusion.
- `carvera_api.py`: the Upload API client. Standard library only, and tested against the real API
  server in `tests/unit/test_fusion_addin.py`.
