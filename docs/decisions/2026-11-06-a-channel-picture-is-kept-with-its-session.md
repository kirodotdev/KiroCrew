# A picture sent on a channel is kept with that session's transcript, so the dashboard still shows it after the turn

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-06

## Decision

A picture the gateway downloads from a channel message is copied into that
session's attachments store (`sessions/<stem>.attachments/`) when the user row
is written. The row then shows the copy instead of the temp path and its "until
this turn ends" note. The copy is kept and deleted with the transcript, in
Incognito and Temporary chats too. There is no per-channel opt-in flag and no
copy into the flat `uploads/` folder, and a path the user typed is never
copied.

## Why

- A channel picture was downloaded to a temp file that the turn deletes, so
  once the turn ended the dashboard showed a dead path where the picture had
  been. This was reproduced on Feishu on `main`.
- The session's attachments store already holds the images a session's rows
  reference. It is bounded per session and deleted with the session, so it
  settles where these pictures live and for how long without a second store.
- Incognito and Temporary chats keep their transcript
  ([2026-09-25 entry](2026-09-25-incognito-and-temporary-chats-keep-their-transcript.md)),
  so a picture kept with its transcript follows the same rule.
- Pull request #10369, which promoted channel pictures into the flat uploads
  folder behind a per-channel flag, was released by its author. This entry
  replaces waiting for it.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/10871#issuecomment-6694220963 --
  the maintainer decision on the issue.
- https://github.com/kirodotdev/KiroCrew/issues/10871#issuecomment-6686467019 --
  the Feishu reproduction on `main`.
- https://github.com/kirodotdev/KiroCrew/pull/10424 and
  https://github.com/kirodotdev/KiroCrew/pull/21229 -- the per-session image
  store and its per-session ceiling.
- https://github.com/kirodotdev/KiroCrew/pull/10369#issuecomment-6434781256 --
  the author releasing #10369.
