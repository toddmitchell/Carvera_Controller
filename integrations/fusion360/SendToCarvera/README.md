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

The button appears on the toolbar at Manufacture → Milling, in its own **CARVERA** panel.

On Windows, [`integrations/windows/install.ps1`](../../windows/install.ps1) does all of this, along with
installing the Controller from this branch and adding it to the Start menu:

```powershell
irm https://raw.githubusercontent.com/toddmitchell/Carvera_Controller/upload-api/integrations/windows/install.ps1 | iex
```

## Use

Click **Send to Carvera** on the toolbar, or right-click in the Manufacture workspace and choose it.
It does the post itself, so there's no need to run Post Process first. Send again after changing
toolpaths and the file on the machine is replaced.

To send part of a job, select setups, folders or toolpaths in the browser (Shift or Cmd for several),
then right-click one and choose **Send to Carvera**. The dialog opens with those ticked.

- **Toolpaths ticked below:** each setup has a *Whole setup* box and a box for each of its toolpaths. Tick
  what you want. What's selected in the browser is ticked first; if nothing is, you get what you sent last
  for this document. A lone setup starts ticked. With several and no history, nothing does, so Send stays
  off until you choose. They always post in the document's order, whatever order you tick them in. You
  also pick a Carvera post from your libraries.
  These use the post's default properties: changes you made in the Post Process dialog aren't carried over.
- **An NC Program** you've kept in the document, chosen from the **Send** list. It uses that program's own
  name, post, properties and tool order. Use one when you need non-default post properties. To keep one
  from Post Process, turn on its option to create the NC Program in the browser.

- **File name:** with everything ticked, it's the document's plain name, so sending replaces the whole
  job. With less ticked, the setup and toolpath names are added to the end, such as `Coin_Trace2.cnc`,
  trimmed to the API's limit of 100 characters. Sending the same selection again replaces that file
  and leaves the whole job alone. You can type over the suggestion for one send, and clearing the field
  brings suggestions back. A name typed with everything ticked becomes the document's plain name.
  Characters the API doesn't allow become `_`, and the dialog shows the result.
- **Post:** for an NC Program, the dialog warns if the post's name doesn't mention Carvera or Makera.
  For toolpaths, only posts with those words in their names are offered.
- **Toolpaths with problems:** posting stops and nothing is sent. Fusion's default would leave those
  operations out without saying so, and the job would be missing cuts.
- **Your document:** the file goes to a temporary folder, not your output folder. An NC Program's output
  folder, "open in editor" and cloud posting settings change for that one post and are put back. For
  toolpaths, a temporary NC Program is created and deleted. Either way, Fusion may mark the document as
  changed.
- **Machine busy:** the file stays queued in the Controller, and the dialog closes after about 10
  seconds. The Controller finishes the upload by itself, and its top bar shows it.
- **A file already loaded:** if the Controller has the replaced file open, load it again before running.

Connection settings are under **Connection** in the dialog. The port, any token, the last post and each document's last send are saved in
`~/.carvera_fusion_addin.json`, readable only by you. The add-in only talks to the Controller on this
computer.

## Code

- `SendToCarvera.py`: the Fusion side (button, dialog, posting). Runs only inside Fusion.
- `selection.py`: which boxes are ticked, and what that sends. Tested in the same file.
- `carvera_api.py`: the Upload API client. Standard library only, and tested against the real API
  server in `tests/unit/test_fusion_addin.py`.
