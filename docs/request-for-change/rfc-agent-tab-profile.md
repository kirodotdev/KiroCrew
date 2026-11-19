---
title: A Settings choice of the Chrome profile agent tabs open in
status: draft
author: neilieva
created: 2026-11-11
last-audited: 2026-11-17
audited-at: 8826a22397
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

Verified at `8826a22397`:

1. Discovery (`browser_harness/discover.py`) picks the person's Chrome and its user data dir. It never launches anything (`discover.py:10`). Its Chrome path comes from `/Applications` or `~/Applications` on macOS, `shutil.which` and then `/opt/google/chrome/chrome` on Linux (`_CHROME_LINUX_BINS`, `_CHROME_LINUX_PATHS`, `discover.py:53-60`), and `%PROGRAMFILES%`, `%PROGRAMFILES(X86)%` or `%LOCALAPPDATA%` on Windows (`_WIN_INSTALL_ROOTS`, `discover.py:64`; `_find_chrome`, `discover.py:203-214`). On Windows with no Chrome, discovery can choose Edge, as kind `edge` (`discover.py:174-175`).
2. For the person's Chrome or Edge, the supervisor only connects. `_start` calls `_user_browser_ws_url` (`browser_harness/supervisor.py:655`), which reads `<user data dir>/DevToolsActivePort` through `_devtools_ws_url` (`supervisor.py:807-820`, `1154-1175`). The relay then holds a loopback TCP connection to the port that file names. This path never reads `BrowserChoice.executable` and never runs the person's browser.
3. `DevToolsActivePort` is written when the person turns on remote debugging in `chrome://inspect` (the error text at `supervisor.py:815-818` asks for that). In Chrome that switch is a saved setting, so a Chrome started later on the same user data dir opens a debugging port and rewrites the file. That part is Chrome's behavior, not code in this repo, and the spike confirms it. The file and `Local State` sit in the person's user data dir, which the agent's account can write. The macOS Seatbelt profile is `(allow default)` with targeted write denies (`sandbox_seatbelt.py:25`).
4. A chat's first op goes through `_bind_target` in `dashboard/handlers/browser_ops.py:430`. It keeps the chat's own tab, else adopts the daemon's current tab if the agent opened it and no other chat owns it, else calls the harness `new_tab` (`browser_ops.py:460`). `tabs new` calls the same helper (`browser_harness/ops.py:1355-1362`).
5. From the browser-harness 0.1.13 source and the issue thread, not this repo: `new_tab` reuses the current tab when that tab is blank, unless the requested URL is `about:blank`. Otherwise it sends `Target.createTarget {url: "about:blank", background: true}` with no `browserContextId`. Chrome then uses `ProfileManager::GetLastUsedProfile()`. `createTarget` accepts only the last-used profile's context id or an off-the-record context, so it can't target another regular profile. The Chromium source for this (`target_handler.cc` `CreateTarget`, `DevToolsBrowserContextManager::GetProfileById`) is quoted in the issue thread, not in this repo.
6. The relay records a target as the agent's when a `Target.createTarget` reply returns (`_route_reply` and `_record_agent_target`, `browser_harness/relay.py:566-592`). Session end closes only ledgered tabs (`CdpRelay.close_agent_targets`, `relay.py:422`). `CdpRelay.send` is the relay's own command path, separate from the daemon's (`relay.py:328`).
7. The supervisor already starts its children in a new session on POSIX and a new process group on Windows (`_spawn`, `supervisor.py:1013-1046`).
8. The repo already resolves the process on the far end of an established loopback TCP connection: `platform_compat.get_tcp_peer_pid` (`platform_compat_ports.py:238`) reads `/proc` socket inodes on Linux, `lsof` from a fixed system path on macOS (`trusted_system_bin`, `platform_compat_ports.py:93`) and the owner-PID table on Windows. It is written for the accepting side. A client finds the server's pid by passing the endpoints swapped, as `_connected_tcp_peer_pid` does (`cli_server_peer.py:331-351`), from a worker thread. `process_executable_path` (`platform_compat_identity.py:146`) reads a pid's image on Linux (`/proc/<pid>/exe`) and macOS, and `None` on Windows. On Linux it strips a trailing ` (deleted)` (`:159`). `darwin_process_argv` (`platform_compat_proctable.py:796`) reads argv on macOS only. On Linux, `/proc/<pid>/cmdline` is read only inside `process_argv_fingerprint` (`platform_compat_identity.py:689`, read at `:710`). No helper returns the vector, so the code PR adds one.

## Decision (from #24085)

- Settings > Browser gets "Profile for agent tabs": "Last used" (the default) plus each profile by display name.
- No `profile` argument on the agent's ops. No per-chat picker in this change.
- With "Last used", nothing changes. No process is launched and no code path below runs.

The setting is stored as the profile's folder name (`Default`, `Profile 2`) under `browser`, because `browserContextId` changes on every Chrome restart.

## Where the option is offered

At most Linux with Google Chrome at `/opt/google/chrome/chrome`, for this version, and only once a maintainer accepts the residual risk in open question 5. Settings shows it as unavailable, with the reason, in every other case:

- Edge (discovery kind `edge`): out of scope for this version. The rules below are written for Chrome's install paths.
- A distro Chromium that discovery finds on `PATH` as kind `user_chrome` (`discover.py:53-57`): its image fails the fixed-path rule.
- macOS: until open question 1 settles how the binary is trusted.
- Windows: until open question 2 settles how the connected browser is tied to its process, and the repo has a Windows image-path and argv reader (Current behavior 8).
- Kiro's own Chromium: it has no regular profiles.

## Design

Every step below fails closed. A missing CDP field (including an absent `browserContextId`), an error reply, a timeout or a failed check fails the op with a reason. Nothing falls back to a bare `createTarget` in the wrong profile.

### The chosen folder

The folder comes from Settings, never from the agent. On use:

1. Its name must be `Default` or `Profile ` followed by a number with no leading zero (`Profile 1`, `Profile 12`). Nothing else is accepted, so no name can carry a quote, a separator, a NUL, a leading `-` or a drive prefix, and the Windows command line has nothing to misparse.
2. It must be a key of `profile.info_cache` in `<user data dir>/Local State`.
3. `lstat` on `<user data dir>/<folder>` must report a plain directory, not a symlink, junction or other reparse point. Its real path must equal the user data dir's real path joined with the name. A link such as `Profile 2 -> Default` fails here.

`Local State` and the folder are agent-writable (Current behavior 3), so these checks only keep the argv well formed and catch an obvious swap. The Profile Path check after launch is what proves the tab's profile.

### Which browser the relay is talking to

`DevToolsActivePort` is agent-writable, so the relay's peer could be an agent-run endpoint that answers CDP and fakes every reply, including `SystemInfo.getProcessInfo` and `chrome://version`. Kiro ignores what the peer says about itself. Before any launch, it identifies the peer from the OS:

1. The relay's upstream socket is an established loopback TCP connection. `get_tcp_peer_pid`, called with the endpoints swapped as Current behavior 8 describes, resolves the pid on its far end for that exact 4-tuple. An unresolvable or ambiguous answer fails closed. The far end of an established connection can't change hands without closing it, and a close fails the op (`on_upstream_closed`, `relay.py:227`).
2. That pid's image must pass the binary rules below. A fake CDP server, or a proxy that forwards to the real Chrome, is a different program, so it fails here.
3. As a sanity check only: that pid's argv must be a Chrome browser process (no `--type=`) whose `--user-data-dir` resolves to the discovered dir, or names none when the discovered dir is the platform default. A same-account process controls its own argv, environment (`CHROME_USER_DATA_DIR`, `LD_PRELOAD`) and memory, so this catches a stale port file, not an attacker.

What this proves, if the spike passes, is that the peer is the genuine Chrome binary. It does not prove the peer is the person's own browser or that its code is unmodified. An agent that starts the genuine Chrome on the same user data dir, with injected code or extra flags, passes every check here. That agent already holds the person's profiles and logins without Kiro, so the launch hands it nothing new. What Kiro still guarantees in that case is the binary rules: it never runs a program the agent planted. That residual risk is open question 5, and a maintainer has to accept it before the option ships anywhere.

### Opening a chat's first tab in the chosen profile

1. Check the folder, the peer and the binary, as described in this document.
2. Run the binary with an argv list, no shell, its own session or process group (as `_spawn` already does, Current behavior 7) and an allowlisted environment: `<binary> --user-data-dir=<dir> --profile-directory=<folder> about:blank#<nonce>`. The nonce is 32 random alphanumerics. Chrome's process singleton hands the URL to the running browser, which opens it in that profile, and the launched process exits.
3. Watch `Target.targetCreated` and `Target.targetInfoChanged` on the relay's own upstream connection for a page whose URL is `about:blank#<nonce>` and that has no `openerId`, with a timeout.
4. Prove the profile: on a relay-owned session, navigate that tab to `chrome://version` and read its Profile Path. Its real path must equal the real path from the folder check. Then navigate it back to `about:blank`. This runs on the relay's own session, not as an agent op, so the agent's URL gate doesn't apply. A tab that fails is closed and never bound.
5. Record the tab through `_record_agent_target`, so it gets the relay-owned session with focus emulation and session end still closes it. Store its `browserContextId` as the chat's context id. Later tabs compare against that value.
6. With a profile chosen, `_bind_target` skips adopting the daemon's current tab and opens through this path. `tabs new` goes through the gateway's own path ("Later tabs in the same chat") instead of the helper's `new_tab`, then uses the helper's `switch_tab` and navigation as today.

The launch has no background mode, so it brings a Chrome window to the front.

The nonce is not a secret from the agent. It is in the launched process's argv, and the relay forwards target events to the daemon. It only keeps Kiro from matching some other tab by accident. The checks that decide what gets bound are the peer check, the tab arriving on the relay's own connection, and the Profile Path check.

### Which binary is run (requirement 1: nothing the agent could have planted)

The launch never uses `BrowserChoice.executable`, `PATH`, or a path built from the home folder or `%LOCALAPPDATA%`. Instead:

1. Open `/proc/<pid>/exe` of the peer once, with `O_PATH`, and pin the pid's start time from `/proc/<pid>/stat`.
2. `fstat` on that descriptor must show the same device and inode as `/opt/google/chrome/chrome` with every symlink resolved. A ` (deleted)` image, left behind by a Chrome update, fails, because its inode no longer matches.
3. That file and every folder above `/opt/google/chrome/chrome` must not be writable by the account the gateway runs as. The agent runs as that account, so anything it can write counts as agent-writable. This means root-owned, no group or world write bit, and no ACL entry granting that account write.
4. Just before the launch, re-confirm that the pid still owns the relay's connection and has the same start time. Then run the binary from the pinned descriptor (`execveat(fd, "", AT_EMPTY_PATH)`), not from any path. A file swapped in afterwards, or a pid the agent recycled after killing Chrome, is never run.
5. The environment is an allowlist. On Linux the starting set is `HOME`, `DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY`, `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`. The spike confirms the handoff works with exactly these. `LD_*` and `CHROME_*` variables never pass.

A person-installed Chrome under the home folder or a `PATH` entry fails these checks, and Settings shows the option as unavailable with the reason.

The rules for macOS and Windows are open questions 1 to 3, not part of this design.

### Which Chrome gets the tab (requirement 2: only the connected one)

The singleton handoff goes to whichever browser owns the singleton for `--user-data-dir`. If the spike passes, the peer check shows the connected browser is the genuine Chrome binary, and in the normal case it is the one on that dir, so the handoff reaches it. The gates that decide the outcome are:

- The tab must arrive on the relay's own upstream connection with the exact nonce URL, and pass the Profile Path check. Nothing else is recorded.
- The launched process runs in its own session or process group. If it is still running after a short timeout, it became a new browser, and Kiro kills the whole group and fails the op.
- After the handoff, the relay's upstream connection must still be open (`on_upstream_closed`, `relay.py:227`), and the peer pid and its start time must be unchanged.

One race is left, and the agent can trigger it on purpose by killing the browser or deleting the `Singleton*` files in the user data dir. If the connected browser exits between the peer check and the handoff, the launched process becomes a full Chrome on the person's profile until the timeout kills it. Remote debugging is a saved setting (Current behavior 3), so that browser can open its own debugging port and rewrite `DevToolsActivePort` while it lives. The window is the timeout, so the timeout should be as short as the spike's measured handoff allows. A window may show briefly. The op fails, because the relay's connection closed. The agent could start that same Chrome itself without Kiro.

### Later tabs in the same chat (requirement 3: nothing reachable from the opener)

Kiro never opens an agent tab with `window.open`.

1. Try `Target.createTarget` with the chat's recorded `browserContextId`. Chrome accepts it only while that profile is the last-used one. Otherwise it fails and opens nothing.
2. On that failure, call `Target.activateTarget` on the chat's own tab, which is in the chosen profile. Activating its window should make that profile the last-used one (`ProfileManager::BrowserCollectionObserver::OnBrowserActivated`). Then retry step 1 once. If the person clicks another profile's window in between, the retry fails cleanly. It never opens a tab in the wrong profile, because the context id is explicit.
3. If the chat's tab is gone or the retry fails, fall back to the launch above.

A tab made by `createTarget` has no opener, so no page can reach it through `window.opener` or a `window.open` return value. Before any tab is bound, it must have no `openerId` and its `browserContextId` must equal the chat's. A tab a page opens itself is never in the ledger, which is the rule today.

## Backward compatibility

The default is "Last used", which is today's path with no launch. A config with no `browser` profile key reads as "Last used". Where the option is unavailable (see "Where the option is offered"), a stored profile reads as "Last used" and Settings says why.

## Security considerations

- The gateway gains one capability: running the person's Chrome binary. The design sections above are the limits on it, one per requirement from #24085.
- `DevToolsActivePort`, `Local State`, the profile folders, the nonce, the peer's argv and environment, and every CDP reply are reachable or forgeable by the agent. None of them is trusted on its own. What is trusted comes from the OS: the peer pid of the relay's connection, that pid's image inode and start time, and the descriptor Kiro runs from.
- No check here stops an agent-started genuine Chrome on the person's user data dir. That agent already has the profiles without Kiro. The design's guarantee is narrower: Kiro never runs a program the agent planted, and never binds a tab outside the chosen profile of the browser it is connected to.
- The tab is bound only when it arrives on the relay's own connection and its Profile Path matches the chosen folder's real path.
- A launch that didn't hand off is killed with its whole process group.
- The agent still can't choose the profile. Only Settings can, so the person decides which logins the agent gets.
- The live browser's rule stays: the agent acts only on tabs in the relay's ledger.

## Alternatives considered

- Reuse discovery's executable: it can come from `PATH` or a home folder (see Current behavior 1).
- Trust `SystemInfo.getProcessInfo` for the browser's pid: the peer answers it, and the peer may be fake.
- Tie the browser to `<user data dir>/SingletonLock`: the lock is a symlink in an agent-writable folder, so the agent can make it name any pid.
- Pass another regular profile's `browserContextId` to `createTarget`: Chrome rejects it.
- `Target.createBrowserContext`: off-the-record, so no logins.
- A bare `createTarget` and then close it if the profile is wrong: shows a tab in the wrong profile first.
- A second Chrome per profile: it can't share the running Chrome's user data dir, so no logins.
- `window.open` with `noopener` from an isolated world, run with `Runtime.evaluate` and `userGesture: true`, for later tabs. `userGesture` gives the whole frame user activation, not only the isolated world. The page's own script could then open a popup and keep a reference to it, or use up the activation so Kiro's call fails. `Target.activateTarget` avoids both.
- Trust the first nonce tab's `browserContextId` as the profile's id: that is circular, since a tab in the wrong profile would set its own expected value.

## Open questions

1. macOS: how is the binary trusted? `/Applications` is `root:admin` with mode `775`, so an admin account can rename `Google Chrome.app` and put another bundle in its place, and a drag-installed bundle is often owned by the installing user. The not-writable rule fails on most Macs, and macOS has no `/proc/<pid>/exe` to run from, so the check and the launch are two steps on one path. One candidate: verify the bundle with `/usr/bin/codesign --verify --strict -R='anchor apple generic and certificate leaf[subject.OU] = "EQHXZ8M8AV"'`, which pins Google's team id, and add a Seatbelt write deny on the bundle path. That deny only covers sandboxed agents, it must cover the bundle's own rename as well as writes inside it, and a genuine Google-signed bundle the agent placed would still pass. The other choice is to leave the option unavailable on macOS. A maintainer should pick.
2. Windows: what ties the connected browser to the process singleton for the user data dir, and what reads a pid's image path and argv there (Current behavior 8 answers `None` on Windows)? If nothing reliable exists, the option stays unavailable on Windows.
3. Windows: is a file and folder ACL check enough for the binary, or should the code PR also verify the Authenticode signer?
4. Edge: should a later version cover it, with Edge's install paths?
5. Is the residual risk acceptable? On Linux the peer lookup reads `/proc` for the exact connection the relay holds, so the agent can't forge which pid owns it, and a fake server or proxy fails the image check. It is unproven that the DevTools socket's far end is Chrome's browser process on every supported Chrome, which the spike tests. Even if it is, nothing ties the peer to the person's own browser or proves its code unmodified: an agent-started genuine Chrome on the same dir, with injected code, passes. Kiro's own launch doesn't add such code: on Linux its environment is an allowlist that never passes `LD_*` or `CHROME_*`, and its argv is the fixed list in this design. A process that starts Chrome as its own child can also attach to it with ptrace, where the kernel's ptrace scope allows it, or pass extra command-line flags such as `--load-extension`, so the checks in this design can't tell a modified genuine Chrome from an unmodified one. The argument above is that such an agent gains nothing from the launch. That holds only where the agent can read and write the person's user data dir and start Chrome. The macOS Seatbelt profile is `(allow default)` (`sandbox_seatbelt.py:25`), so it allows both unless the dir is in a masked or read-only tree. Whether the Linux namespace sandbox does is unconfirmed. A maintainer should decide whether that is enough, or whether the option should not ship. On macOS, whether Chrome's hardened runtime ignores `DYLD_INSERT_LIBRARIES` is unverified, and how the binary is trusted there is open question 1. macOS (`lsof`) and Windows (owner-PID table) would need their own review.
6. The daemon's own tab at connect time is still a bare `createTarget` in the last-used profile. I'd leave it, since it stays on `about:blank` and no chat binds it while a profile is chosen.

## Spike items for the code PR

The code PR carries these results for each platform it enables, and updates `docs/system-specs/modules/browser.md` in the same diff:

- `get_tcp_peer_pid`, called with the endpoints swapped, on the relay's upstream connection returns the Chrome browser process's pid, and that pid's argv shows the expected `--user-data-dir` (or none for the default dir).
- Running from an `O_PATH` descriptor of `/proc/<pid>/exe` with `execveat` hands off to the running browser like a normal launch.
- A Chrome started later on the same user data dir with remote debugging turned on opens a port and rewrites `DevToolsActivePort`.
- A tab in a profile that isn't the last-used one shows up in `Target.targetCreated` on the existing connection, and the relay can attach to it and drive it (Page, Runtime, Input, screencast). The issue thread notes that tabs in different profiles report different `browserContextId` values, but nobody has driven one through the relay yet.
- `Target.createTarget` still rejects another loaded regular profile's `browserContextId` on current Chrome. If it doesn't, that path replaces the launch.
- `about:blank#<nonce>` arrives as given, and whether it opens as a tab in the profile's last window or as a new window.
- The launched process exits after the handoff, how long it takes, and that it works with exactly the allowlisted environment.
- `chrome://version` can be loaded and its Profile Path read over a relay-owned session.
- `Target.activateTarget` on a tab makes its profile the last-used one, so a following `createTarget` with that `browserContextId` succeeds.
- `targetCreated` for the nonce tab carries the URL, or only `targetInfoChanged` does.

## Test plan

Unit tests for:

- the folder checks, including a name outside `Default` and `Profile <n>`, and a symlinked folder;
- the peer check refusing an unresolvable peer, a peer whose image fails the binary rules, a peer on another user data dir, and a peer whose pid or start time changed before the launch;
- each rejected binary path kind and an accepted one;
- the argv list and the environment allowlist;
- matching and recording the nonce target, and ignoring one that arrives anywhere but the relay's connection;
- the Profile Path check rejecting a wrong profile, compared on real paths;
- killing the process group of a launch that didn't hand off;
- the fallback order for later tabs;
- a tab with an `openerId` or a different context never being bound;
- "Last used" keeping today's path, and the option reading as unavailable on Edge, macOS and Windows.

The relay tests' fake browser needs `browserContextId` and `openerId` on its targets. By hand on Linux with two profiles.
