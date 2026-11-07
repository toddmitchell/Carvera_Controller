"""Which setups and toolpaths are ticked in the Send to Carvera dialog. No Fusion imports.

The tree is a list of (setup id, [operation ids]), in the document's order. Ids are Fusion's operation
ids, which stay the same when things are reordered, so they can be remembered between sessions.
"""

from __future__ import annotations


def expand(tree: list, ids) -> set:
    """Ticks for `ids`: a setup ticks all its toolpaths, and all its toolpaths tick the setup."""
    wanted = set(ids)
    ticked = set()
    for setup, operations in tree:
        if setup in wanted or (operations and wanted.issuperset(operations)):
            ticked.add(setup)
            ticked.update(operations)
        else:
            ticked.update(op for op in operations if op in wanted)
    return ticked


def initial(tree: list, browser_ids, saved_ids) -> set:
    """What selected in Fusion's browser comes first, then the last send. A lone setup is ticked to
    start with. With several and no history nothing is, so a job is always a deliberate choice."""
    for ids in (browser_ids, saved_ids):
        ticked = expand(tree, ids)
        if ticked:
            return ticked
    return expand(tree, [tree[0][0]]) if len(tree) == 1 else set()


def to_send(tree: list, ticked) -> list:
    """The ids to post, in document order: a whole setup as the setup, otherwise its ticked toolpaths."""
    ids = []
    for setup, operations in tree:
        if setup in ticked:
            ids.append(setup)
        else:
            ids.extend(op for op in operations if op in ticked)
    return ids


def sends_everything(tree: list, ticked) -> bool:
    """Is every setup that has toolpaths ticked as a whole?"""
    return all(setup in ticked for setup, operations in tree if operations)


def file_stem(base: str, tree: list, ticked, names: dict) -> str:
    """The file name to suggest. Everything gets the plain name, so sending it again replaces the job.
    Anything less says what's in it, so it can't replace the whole job, and sending the same selection
    again replaces only that file. The API's length limit trims the end (carvera_api.safe_name)."""
    if sends_everything(tree, ticked):
        return base
    return "_".join([base, *(names[i] for i in to_send(tree, ticked))])
