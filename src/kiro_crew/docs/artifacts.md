# Artifacts

An artifact is something the agent generated that you want to keep — a widget, an
HTML page, a document. Saving it gives it a stable handle you can come back to in
a later conversation, instead of losing it to the chat scrollback.

The chat side panel has an **Artifacts** tab listing what you have saved. Open one
to view it, and ask the agent to iterate on it by name.

## Saving and iterating

Ask to save something the agent just rendered, and it comes back with a handle.
Widgets rendered in chat are also registered as artifacts automatically, each one
as its part of the response finalizes rather than when the whole message ends — so
a widget you did not explicitly save still appears in the Artifacts tab, and an
early widget is filed before a long answer finishes. Automatic entries are
unpinned, and the oldest unpinned ones are pruned once there are many of them, so
star a widget in chat, or ask the agent to save it, when you want to keep it for
good.

**Incognito and temporary sessions never register a widget, and cannot save one
either.** Saving is refused outright in those modes, so a widget rendered there
exists only in the conversation and is gone when the session ends. That is the
point of them: nothing is left behind. Render it in an ordinary session if you
need to keep it.

Every agent edit to a saved artifact creates a **new version**, like a commit. You
can list the versions, read an older one, or revert the live state to any of them.
A revert is itself recorded as a new version tagged as a rollback, so the timeline
shows what happened rather than quietly rewinding.

To see what changed between two versions, open the artifact and pick **Compare with**
next to the version picker. The older state sits on the left, the newer on the right
(Live counts as the newest), each column is named, and the changed lines are marked. On a
narrow screen, or when the diff layout in Display settings is set to unified, the two states
are drawn in one column instead. Choose **Stop comparing**
to go back to the document. Comparison works for text artifacts and is not offered for
images, web apps or while you are editing.

## Quick edits

On a document artifact (markdown, plain text or HTML), the page's **More** menu has a
**Quick edits** submenu: Shorter, Longer, Simpler wording, Proofread, and Change tone
(More formal or More casual). Picking one sends a single preset instruction to the
artifact's companion chat, for the text you had selected or, with no selection, the
whole document. The chat shows only a short line such as "Shorter · selected passage";
the instruction itself travels as background context. The agent makes the change
through its normal artifact update, so it lands as a new version you can read or
revert, and the menu says so. A line on the page follows the edit: working, saved as
the new version, or finished without a new version. While the agent is still busy in
that chat, a quick edit is not sent and the page asks you to try again when it
finishes. A selection made
with the mouse is kept while you open the menu, and the menu quotes the start of it so
you can see which passage will be rewritten. If the comments panel holds a comment
you have not sent, you are asked before the chat panel replaces it. Nothing is sent
until you pick an item, and widgets and apps do not show the menu.

## Organizing

Artifacts can be filed into folders, which nest. Moving an artifact or renaming a
folder changes nothing about the content. Deleting a folder is safe by default —
its children move up to the parent and only the folder itself goes. Deleting the
whole subtree, artifacts included, takes an explicit choice and cannot be undone
through the same path, so you should be told the count before it happens.

## Comments

An artifact carries comment threads, optionally anchored to a specific passage.
The agent can post, reply, and mark a thread as **awaiting your review** once it
believes it has addressed the point — but it cannot resolve a thread. Resolving is
yours.

The agent may delete a comment thread only where it demonstrably carried out an
unambiguous instruction ("fix this typo") and did so. For anything that is a
judgement call it marks the thread for review instead, so you get to check.
Comments synced from an external provider cannot be deleted by the agent at all.

## Publishing one

A saved web artifact can be deployed to a public HTTPS URL on your own AWS
account. The agent's part is **preview only**: it reports the scan result and what
the deploy would do, and never executes it. The final confirmation happens in the
dashboard.

Read [Web deploy](deploy-web.md) before doing this — a deployed artifact is
world-readable.

## Related docs

- [Web deploy](deploy-web.md): the public-URL path, and what world-readable means
- [Dashboard](dashboard.md): the chat side panel and its tabs
- [Knowledge library](knowledge-library-how-it-works.md): search across saved artifacts and documents
