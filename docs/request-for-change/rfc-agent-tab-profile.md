---
title: A Settings choice of the Chrome profile agent tabs open in
status: draft
author: neilieva
created: 2026-11-11
last-audited: 2026-11-11
audited-at: b0c0fb5f12
doc-pr: null
implementation-prs: []
tracking-issues: [24085]
supersedes: []
superseded-by: []
---

# RFC: A Settings choice of the Chrome profile agent tabs open in

- Status: `draft`. Acceptance is requested from a maintainer. Nothing is built.
- Tracking issue: [#24085](https://github.com/kirodotdev/KiroCrew/issues/24085)

This document settles one thing before any code: Kiro starting the person's own Chrome to open a tab in a chosen profile. That moves a trust boundary, so the maintainer decision on #24085 asked for it to be argued here first. The product shape is already decided there and is not reopened here.

## Problem

When the live browser drives the person's Chrome, every agent tab opens in whichever regular profile was used last. There is no way to say "agent tabs go in my work profile". The person has to click a window of that profile before each chat, and any later tab follows whatever window they clicked last.

## Current behavior

Verified at `b0c0fb5f12`:

1. Discovery (`browser_harness/discover.py`) picks the person's Chrome and its user data dir. It never launches anything. Its Chrome path comes from `/Applications` or `~/Applications` on macOS, `shutil.which` and then `/opt/google/chrome/chrome` on Linux (`_CHROME_LINUX_BINS`, `_CHROME_LINUX_PATHS`), and `%PROGRAMFILES%`, `%PROGRAMFILES(X86)%` or `%LOCALAPPDATA%` on Windows (`_find_chrome` and `_WIN_INSTALL_ROOTS`).
2. For the person's Chrome, the supervisor only connects. `_start` calls `_user_browser_ws_url` in `browser_harness/supervisor.py`, which reads `<user data dir>/DevToolsActivePort` through `_devtools_ws_url` and hands that websocket to the relay. That path never reads `BrowserChoice.executable`. Kiro has never run the person's Chrome binary.
3. `DevToolsActivePort` and `Local State` sit in the person's user data dir, which the agent's account can write.
4. A chat's first op goes through `_bind_target` in `dashboard/handlers/browser_ops.py`. It keeps the chat's own tab, else adopts the daemon's current tab if the agent opened it and no other chat owns it, else calls the harness `new_tab`. `tabs new` in `browser_harness/ops.py` calls the same helper.
5. browser-harness 0.1.13 `new_tab` reuses the current tab when it is blank and the URL isn't `about:blank`. Otherwise it sends `Target.createTarget {url: "about:blank", background: true}` with no `browserContextId`. Chrome then uses `ProfileManager::GetLastUsedProfile()`. `createTarget` accepts only the last-used profile's context id or an off-the-record context, so it can't target another regular profile. The Chromium source for this (`target_handler.cc` `CreateTarget`, `DevToolsBrowserContextManager::GetProfileById`) is quoted in the issue thread.
6. The relay records a target as the agent's when a `Target.createTarget` reply returns (`_route_reply` and `_record_agent_target` in `browser_harness/relay.py`). Only ledgered tabs can be bound or switched to, and session end closes only ledgered tabs (`CdpRelay.close_agent_targets`). `CdpRelay.send` is the relay's own command path, separate from the daemon's.

## Decision (from #24085)

- Settings > Browser gets "Profile for agent tabs": "Last used" (the default) plus each profile by display name.
- No `profile` argument on the agent's ops. No per-chat picker in this change.
- With "Last used", nothing changes. No process is launched and no code path below runs.

The setting is stored as the profile's folder name (`Default`, `Profile 2`) under `browser`, because `browserContextId` changes on every Chrome restart.

## Design

Every step below fails closed. A missing CDP field (including an absent `browserContextId`), an error reply, a timeout or a failed check fails the op with a reason. Nothing falls back to a bare `createTarget` in the wrong profile.

### The chosen folder

The folder comes from Settings, never from the agent. On use, it must be a key of `profile.info_cache` in `<user data dir>/Local State` and a single path component: not `.` or `..`, no `/` or `\`, no drive prefix, no NUL, no leading `-`. The resolved directory must be a direct child of the user data dir. `Local State` is agent-writable (Current behavior 3), so this check only keeps the argv well formed. The profile check after launch is what proves the tab's profile.

### Opening a chat's first tab in the chosen profile

1. Check the binary and the browser that holds the profile, as the next two sections describe.
2. Run the binary with an argv list, no shell and an allowlisted environment: `<binary> --user-data-dir=<dir> --profile-directory=<folder> about:blank#<nonce>`. The nonce is 32 random alphanumerics. Chrome's process singleton hands the URL to the running browser, which opens it in that profile, and the launched process exits.
3. Watch `Target.targetCreated` and `Target.targetInfoChanged` on the relay connection for a page whose URL is `about:blank#<nonce>` and that has no `openerId`, with a timeout.
4. Prove the profile: on a relay-owned session, navigate that tab to `chrome://version` and read its Profile Path. It must equal `<user data dir>/<folder>`. Then navigate it back to `about:blank`. This runs on the relay's own session, not as an agent op, so the agent's URL gate doesn't apply. A tab that fails is closed and never bound.
5. Record the tab through `_record_agent_target`, so it gets the relay-owned session with focus emulation and session end still closes it. Store its `browserContextId` as the chat's context id. Later tabs compare against that value.
6. With a profile chosen, `_bind_target` skips adopting the daemon's current tab and opens through this path. `tabs new` goes through the gateway's own path below instead of the helper's `new_tab`, then uses the helper's `switch_tab` and navigation as today.

This launch has no background mode, so it brings a Chrome window to the front.

The nonce is not a secret from the agent. It is in the launched process's argv, and the relay forwards target events to the daemon. It only keeps Kiro from matching some other tab by accident. If the agent opened the nonce URL itself (for example by running Chrome with its own `--profile-directory`), the Profile Path check in step 4 still rejects a tab in the wrong profile.

### Which binary is run (requirement 1: nothing the agent could have planted)

The launch never uses `BrowserChoice.executable`, `PATH`, or a path built from the home folder or `%LOCALAPPDATA%`. Instead:

1. Ask the connected browser for its own process id with `SystemInfo.getProcessInfo` (the entry whose `type` is `browser`), sent with `CdpRelay.send`.
2. Get that process's image path from the OS: `/proc/<pid>/exe` on Linux, `proc_pidpath` on macOS, `QueryFullProcessImageNameW` on Windows.
3. The path, with every symlink resolved, must equal one fixed install path:
   - macOS: `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
   - Linux: `/opt/google/chrome/chrome`
   - Windows: `<ProgramFiles>\Google\Chrome\Application\chrome.exe` or the `(x86)` one, with the roots read from the registry, not the environment.
4. On Linux and Windows, the resolved file and every folder above it must not be writable by the account the gateway runs as. The agent runs as that account, and the macOS Seatbelt profile is `(allow default)` with targeted write denies (`sandbox_seatbelt.py`), so anything that account can write counts as agent-writable. On Linux this means root-owned, no group or world write bit, and no ACL entry granting that account write. On Windows it means the file and folder ACLs grant that account no write or delete.
5. On macOS, rule 4 fails on most machines (open question 1). The code signature must verify with `/usr/bin/codesign --verify --strict -R='anchor apple generic and certificate leaf[subject.OU] = "EQHXZ8M8AV"'` on the bundle, which pins Google's team id. Whether that is enough is open question 1.
6. On Linux, Kiro runs `/proc/<pid>/exe` of the connected browser instead of the path. That is the inode the running browser was started from, so a file swapped in afterwards is never run. macOS and Windows have no equivalent, so there the check and the launch are two steps on one path.
7. The environment is an allowlist: the display and session variables Chrome needs to reach the running browser, and nothing else. `LD_*`, `DYLD_*` and `CHROME_*` variables never pass.

A person-installed Chrome under `~/Applications`, `%LOCALAPPDATA%` or a `PATH` entry fails these checks. Settings then shows the option as unavailable with the reason, and "Last used" keeps working.

### Which Chrome gets the tab (requirement 2: only the connected one)

`DevToolsActivePort` is agent-writable (Current behavior 3). A stale or rewritten file could point the relay at a different browser than the one holding the person's profile. Then the singleton would have no owner, and the launch would start a full Chrome on the person's profile. So:

- Before the launch, on macOS and Linux, `<user data dir>/SingletonLock` must be a symlink to `<this host>-<pid>`, with the same pid `SystemInfo.getProcessInfo` returned. That ties the connected browser to the singleton that will receive the URL. Windows has no `SingletonLock`, and how to make the same tie there is open question 2.
- `--user-data-dir` is that same folder, so the singleton handoff goes to that browser.
- The launched process is a direct child of the gateway. If it is still running after a short timeout, or `SingletonLock` now names its pid, it became a new browser. Kiro kills it and fails the op. That browser has no debugging flags, so it can never become the connected one.
- After the handoff, the connected browser's pid must be unchanged.
- Only a target that shows up on the relay's own connection with the exact nonce URL, and passes the Profile Path check, is recorded.

One race is left. If the connected browser exits between the lock check and the handoff, the launched process becomes a new Chrome on the person's profile until the timeout kills it. That browser holds the person's logins but has no debugging connection, so the agent can't reach it. A window may show briefly.

### Later tabs in the same chat (requirement 3: nothing reachable from the opener)

Kiro never opens an agent tab with `window.open`.

1. Try `Target.createTarget` with the chat's recorded `browserContextId`. Chrome accepts it only while that profile is the last-used one. Otherwise it fails and opens nothing.
2. On that failure, call `Target.activateTarget` on the chat's own tab, which is in the chosen profile. Activating its window should make that profile the last-used one (`ProfileManager::BrowserCollectionObserver::OnBrowserActivated`). Then retry step 1 once. If the person clicks another profile's window in between, the retry fails cleanly. It never opens a tab in the wrong profile, because the context id is explicit.
3. If the chat's tab is gone or the retry fails, fall back to the launch above.

A tab made by `createTarget` has no opener, so no page can reach it through `window.opener` or a `window.open` return value. Before any tab is bound, it must have no `openerId` and its `browserContextId` must equal the chat's. A tab a page opens itself is never in the ledger, which is the rule today.

## Backward compatibility

The default is "Last used", which is today's path with no launch. A config with no `browser` profile key reads as "Last used". Kiro's own Chromium has no regular profiles, so the option is hidden when it is the chosen browser.

## Security considerations

- The gateway gains one capability: running the person's Chrome binary. The three design sections above are the limits on it, one per requirement from #24085.
- `DevToolsActivePort`, `Local State` and the nonce are all reachable by the agent. The design treats them as untrusted. The binary is tied to the connected process, the process to `SingletonLock`, and the tab to its Profile Path.
- On macOS and Windows, a swap between the binary check and the launch is only blocked where the agent can't write the path. On macOS that depends on open question 1.
- The agent still can't choose the profile. Only Settings can, so the person decides which logins the agent gets.
- The live browser's rule stays: the agent acts only on tabs in the relay's ledger.

## Alternatives considered

- Reuse discovery's executable: it can come from `PATH` or a home folder (see Current behavior 1).
- Pass another regular profile's `browserContextId` to `createTarget`: Chrome rejects it.
- `Target.createBrowserContext`: off-the-record, so no logins.
- A bare `createTarget` and then close it if the profile is wrong: shows a tab in the wrong profile first.
- A second Chrome per profile: it can't share the running Chrome's user data dir, so no logins.
- `window.open` with `noopener` from an isolated world, run with `Runtime.evaluate` and `userGesture: true`, for later tabs. `userGesture` gives the whole frame user activation, not only the isolated world. The page's own script could then open a popup and keep a reference to it, or use up the activation so Kiro's call fails. `Target.activateTarget` avoids both.
- Trust the first nonce tab's `browserContextId` as the profile's id: that is circular, since a tab in the wrong profile would set its own expected value.

## Open questions

1. macOS: `/Applications` is `root:admin` with mode `775`, so an admin account can rename `Google Chrome.app` and put another bundle in its place. A drag-installed bundle is also often owned by the installing user. So rule 4 fails on most Macs. I see two ways out, and a maintainer should pick one. (a) Accept rule 5 (the pinned code signature) in place of rule 4 on macOS, and add a Seatbelt write deny on the bundle path. That only covers sandboxed agents, and a genuine Google-signed bundle the agent placed would still pass. (b) Keep rule 4 strict and leave the option unavailable on those Macs.
2. Windows: what ties the connected browser to the process singleton for the user data dir, in place of `SingletonLock`? If nothing reliable exists, should the option be unavailable on Windows?
3. Is the Windows ACL check enough, or should the code PR also verify the Authenticode signer?
4. Edge is not in the maintainer decision. Is it out of scope for this version, or covered by the same rules with `<ProgramFiles(x86)>\Microsoft\Edge\Application\msedge.exe`?
5. The daemon's own tab at connect time is still a bare `createTarget` in the last-used profile. I'd leave it, since it stays on `about:blank` and no chat binds it while a profile is chosen.

## Spike items for the code PR

The code PR carries these results for macOS and Windows, and updates `docs/system-specs/modules/browser.md` in the same diff:

- A tab in a profile that isn't the last-used one shows up in `Target.targetCreated` on the existing connection, and the relay can attach to it and drive it (Page, Runtime, Input, screencast). The issue thread notes that tabs in different profiles report different `browserContextId` values, but nobody has driven one through the relay yet.
- `Target.createTarget` still rejects another loaded regular profile's `browserContextId` on current Chrome for macOS and Windows. If it doesn't, that path replaces the launch.
- `SystemInfo.getProcessInfo` answers on a connection to the person's Chrome and lists the browser process with its pid.
- On macOS, `SingletonLock` names the same pid while Chrome runs.
- `about:blank#<nonce>` arrives as given, and whether it opens as a tab in the profile's last window or as a new window.
- The launched process exits after the handoff, and how long it takes.
- `chrome://version` can be loaded and its Profile Path read over a relay-owned session.
- `Target.activateTarget` on a tab makes its profile the last-used one, so a following `createTarget` with that `browserContextId` succeeds.
- `targetCreated` for the nonce tab carries the URL, or only `targetInfoChanged` does.

## Test plan

Unit tests for: the folder checks, each rejected binary path kind and an accepted one, the `SingletonLock` pid match, the argv list and the environment allowlist, matching and recording the nonce target, the Profile Path check rejecting a wrong profile, killing a launch that didn't hand off, the fallback order for later tabs, a tab with an `openerId` or a different context never being bound, and "Last used" keeping today's path. The relay tests' fake browser needs `browserContextId` and `openerId` on its targets. By hand on macOS and Windows with two profiles.
