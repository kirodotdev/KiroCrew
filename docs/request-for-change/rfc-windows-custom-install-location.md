---
title: The Windows installer lets you choose where Kiro Crew is installed
status: draft
author: fankaibo
created: 2026-11-20
last-audited: 2026-11-20
audited-at: cdb601611f
doc-pr: null
implementation-prs: []
tracking-issues: [11975]
supersedes: []
superseded-by: []
---

# RFC: Choosing the install location in the Windows installer

> **Status:** `draft`. Nothing built; this document changes no code. Written from
> reading the source at `cdb601611f`. Nothing here was run on a Windows machine,
> so every statement about installer behaviour is a reading of source, and
> Phase 0 exists to turn the open ones into observations.

## Summary

The Windows installer shows a page where the user picks the install folder. The
default stays `%LOCALAPPDATA%\Programs\KiroCrew` (all users: `Program Files`). A
folder the user picks goes through the same ownership rule the installer already
applies to a `/D=` argument, so uninstalling never removes a folder the installer
did not create. Updates keep the folder they were installed into and skip the
page.

## Motivation

A user on a machine with a small system drive cannot keep the program off `C:`.
The installer offers only the install scope (just me or all users) and never a
folder. [#11975](https://github.com/kirodotdev/KiroCrew/issues/11975) asks for
this and was closed as stale on 2026-11-06 with no maintainer decision; another
request was added to it on 2026-11-20. A system drive is often small and hard to
expand, and the program directory is a part the user cannot move afterwards.

Current state, verified at `cdb601611f`:

- `website/electron/package.json` sets `build.nsis.oneClick: false`,
  `perMachine: false` and `allowToChangeInstallationDirectory: false`.
  `website/electron/test/packaging.test.js` asserts the last value, so the
  current behaviour is deliberate and guarded by a test.
- The installer is already built to accept a folder from outside.
  `KiroEnsureAppInstallDir` in `website/electron/build/installer.nsh` forces the
  last path element to be `KiroCrew` and nests below any directory that already
  exists. It does so because electron-builder's generated uninstaller removes
  `$INSTDIR` recursively, so the installer must only adopt a directory it
  created.
- That path is exercised in CI, but only as a silent install:
  `.github/scripts/test-windows-installer.ps1` and `scripts/smoke-windows-install.ps1`
  run `/S /currentuser /D=<root>`. No document tells a user about `/D=`, and no
  test drives the interactive wizard with it.
- An update never relocates the install. The registered `InstallLocation` is read
  by electron-builder's `initMultiUser`, and `KiroEnsureAppInstallDir` returns
  early for an update over a directory that holds the app's executable.
- The update path moves every installed file to
  `%TEMP%\nsaXXXX.tmp\old-install\<relative path>`. `packaging/check_windows_path_budget.py`
  therefore limits a path relative to the install root to 186 characters
  (259 − 48 − 25) and states that this budget does not depend on the install
  root.

What stays on `C:` after this RFC: the data home `~/.kiro/crew`, which
`KIROCREW_HOME` already relocates for a fresh start, and the files beside it that
`docs/guides/install.md#folders-beside-the-data-home` lists. The user who filed
#11975 measured about 866 MB for the program and about 670 MB for the LibreOffice
runtime it downloads later (a user report on `0.7.1-insider.2`, not a measurement
made for this RFC). This RFC moves the first of those and leaves the second.

## Goals

- A user can choose the install folder in the installer's own wizard.
- The default folder, the scope choice and the per-user, no-UAC default do not change.
- A chosen folder is subject to the same ownership rule as `/D=`.
- A silent install, an update and an uninstall behave as they do today.

## Non-goals

- Moving the data home, the LibreOffice runtime, downloaded models or any other
  on-demand dependency. Those need their own decision about where they are
  looked up and how existing data moves, and #11975 already lists them.
- Migrating an existing install to another folder. The way to move is
  uninstall (which keeps `~/.kiro/crew`) and install again.
- Offering a portable (unzip and run) build.

## Design

### 1. Show the directory page

Set `build.nsis.allowToChangeInstallationDirectory` to `true`. The template
(`assistedInstaller.nsh` in electron-builder, read on its default branch; this
repo pins `26.15.3`, which was not diffed) then inserts the standard directory
page after the install-mode page and before the progress page, and wraps it in
`skipPageIfUpdated`, so an update still shows no decision page. The field starts
at the `$INSTDIR` that `customInit` resolves today.

### 2. Run the chosen folder through the `/D=` rule

The template's own cleanup after the page appends `\KiroCrew` only when the path
does not already contain the text `KiroCrew` anywhere, so `D:\Backup of KiroCrew`
would be adopted as it is, and the generated uninstaller would later remove it
recursively. The installer must not rely on that. The folder the user picked must
pass through `KiroEnsureAppInstallDir`, the way the install-mode page's result
already does through `KiroValidateInstallDirAfterMode`.

The `customPageAfterChangeDir` macro already sits between the directory page and
the progress page. One constraint shapes the hook: the template notes that a
leave callback on the directory page does not see the folder the user chose, so
the check belongs in the progress page's pre callback, where the template already
attaches `instFilesPre`. A page takes one pre callback, so the repo's callback
would have to call `instFilesPre` and then validate. Whether that compiles and
orders as intended is a Phase 1 task; this document has not run `makensis`.

A refused folder (a file, an unwritable path, a root that is too long) reuses the
existing failure shape: a message in an interactive run and exit code 2.

### 3. Refuse a root that is too long

A fresh install writes `<root>\<relative path>` directly, so the root plus the
longest shipped relative path must stay within 259 characters. With the 186
character relative budget, a root longer than 72 characters (259 − 186 − 1) can
break the install. The default root is at most 61 characters for a 20-character
account name. The installer refuses a longer root with a message that names the
limit. Whether the limit is the fixed 72 or the longest relative path measured at
build time is an open question.

### 4. Tests and docs

- The `allowToChangeInstallationDirectory` assertion in `packaging.test.js`
  expects `true`, and a new assertion pins the order: directory page, then
  validation, then progress page.
- The comment in `packaging.test.js` that explains the shortcut hazard says that
  without the define, `setIsTryToKeepShortcuts` always yields `true`. With the
  define, electron-builder's template yields `false` for a run that is not an
  update, so a plain reinstall over an existing install no longer asks the old
  uninstaller to keep shortcuts. The update path is unchanged. The comment is
  rewritten, and the hazard it describes (uninstalling one channel deregistering
  the AppUserModelID the other channel's shortcuts carry) is re-checked on a real
  machine with a stable and a nightly install side by side.
- `docs/guides/windows-install.md`, the "Assisted installer, per user by default"
  entry, states that the wizard offers the folder and how a refused folder fails.

## Migration plan

### Phase 0: observe today's behaviour on a Windows machine

No code. Build or download an installer from `main` and record, on a real
Windows machine: (a) an interactive run with `/D=D:\Apps` installs into
`D:\Apps\KiroCrew`; (b) a later update, run with `--updated`, keeps that folder
and creates no second install; (c) uninstall removes `D:\Apps\KiroCrew` and
leaves `D:\Apps` and its other contents; (d) the same run with `/D=` pointing at an existing non-empty
folder installs into a new `KiroCrew` child and leaves the folder's contents alone.

Exit criteria: the four results are written into the pull request that
documents `/D=` in `docs/guides/windows-install.md` as the supported route for
users who cannot wait for the page. If any of (a) to (d) fails, that failure
is filed as its own issue and Phase 1 is blocked on it.

### Phase 1: the directory page

Design sections 1 to 4. Entry: this RFC is `accepted`, and Phase 0 passed.

Exit criteria, each testable:

- `npm test` in `website/electron` passes with the changed assertions.
- The Windows installer workflow builds the installer and its silent `/D=`
  cases still pass unchanged.
- On a real Windows machine, the wizard shows the page on a fresh install,
  `D:\Apps` becomes `D:\Apps\KiroCrew`, an existing non-empty folder is nested
  under, a 73-character root is refused with exit code 2, and an update shows
  no page and keeps the folder.
- A stable and a nightly install side by side survive uninstalling one of them
  with the other's shortcuts still resolving.

## Backward compatibility

Compatible for what exists. An installed copy keeps its registered folder and an
update skips the page. A script that passes `/S /D=<root>` reaches the same
function it reaches now. The default folders are unchanged.

One behaviour moves: a non-update run over an existing install no longer asks
the old uninstaller to keep shortcuts (design section 4). Nothing is removed
from a user's data: `deleteAppDataOnUninstall` stays `false`.

## Security considerations

- **The uninstaller removes `$INSTDIR` recursively.** Letting the user type a
  folder is the moment this matters. Design section 2 keeps the rule that the
  installer only owns a folder it created, and Phase 1 tests it with an existing
  non-empty folder.
- **All-users scope with a folder a standard user can write.** An elevated
  install into a folder that standard users can modify lets one of them replace
  a binary that another user or an elevated process later runs. The per-user
  default under `%LOCALAPPDATA%` is private to the user. The inherited
  permissions on a data volume's root were not checked for this RFC. Whether the
  directory page is offered for the all-users scope, or the installer sets a
  restricted ACL on the folder it creates, is an open question.
- Per-user installs on a data volume run with the user's own rights, as today.
- No new network access and no credentials are involved.

## Alternatives considered

- **Only document `/D=`.** It works for the program directory with no code
  change, and Phase 0 checks it. It stays a command-line switch with NSIS's
  rule that it must be the last argument and must not be quoted, and CI covers
  only the silent form. It is the interim answer, not the fix.
- **A junction from the default folder to another drive.** It needs no installer
  change, but the update path moves every file under the install root, and this
  RFC did not test how that behaves across a junction. Not recommended without a test.
- **Bring back a custom destination control.** `docs/guides/windows-install.md`
  says an earlier custom installer had one and that the native flow replaced it.
  The native page carries the localisation and the update skip for free.
- **A portable build.** A different distribution shape with its own update story;
  out of scope.

## Open questions

1. Is a visible directory page acceptable as the default shape for every user, or
   should it sit behind an "advanced" choice? This is a product-shape decision
   for a maintainer.
2. Should the all-users scope offer the page, or should the installer restrict
   the ACL on the folder it creates? See Security considerations.
3. Is the root-length limit the fixed 72, or measured from the shipped payload at
   build time?
4. Can the progress page's pre callback call `instFilesPre` and then validate, in
   the pinned electron-builder `26.15.3`? Answered by compiling in Phase 1.
5. Where should the follow-up about the data home and on-demand dependencies be
   tracked, given #11975 is closed? Suggested: reopen it for those parts and
   track this RFC's part separately.
