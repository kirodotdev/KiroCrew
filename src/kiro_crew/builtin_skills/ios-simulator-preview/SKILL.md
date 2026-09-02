---
name: ios-simulator-preview
description: Show a live iOS Simulator in the dashboard's Browser side panel while building or testing an iOS app. Use when the user asks to preview an iOS/SwiftUI app, run an app in the simulator, "show me the app", test in the iOS simulator, or watch you drive an iOS app. macOS + Xcode only.
triggers: ios simulator, iphone simulator, iphone preview, swiftui preview, run in simulator, xcode app preview, mirror simulator, show me the app on iphone
---

# iOS Simulator Preview (Browser-tab mirror)

Mirror a running iOS Simulator into KiroCrew's Browser side panel so the user
watches the app — and you driving it — live next to the chat.

Architecture, end to end, all local: **simulator → `serve-sim` (loopback HTTP)
→ the `kirocrew:preview` marker → Browser panel iframe.** Nothing leaves the
machine. This mirrors OpenAI Codex's `ios-simulator-browser` approach (same
`serve-sim` mirror) with one substitution: Codex opens the URL in its in-app
browser via a browser tool, while KiroCrew's panel is driven by the hidden
preview marker (see the `web-preview` skill).

## Requirements

- **macOS only.** Apple's simulator runs nowhere else. On Linux/Windows say so
  and stop — do not attempt a fallback.
- Xcode with an iOS platform installed, and the license accepted. `xcrun simctl
  list devices` must work.
- Node/`npx`. The launcher pins `--registry=https://registry.npmjs.org` because
  a local npm config may default to an authenticated internal registry (which
  401s on a public package). The `serve-sim` version is **pinned** in the
  launcher (`SERVE_SIM_VERSION`) rather than floated, so a start never
  auto-executes an unreviewed release; bumping it is a deliberate change.
- First `start` takes a couple of minutes (npx download + device boot); later
  starts are fast.

## Resolve the launcher path once

```bash
SKILL_DIR="${KIROCREW_HOME:-$HOME/.kiro/crew}/skills/ios-simulator-preview"
```

Call the script by path; do not `cd` into the skill folder.

`KIROCREW_HOME`, when set, must be an ABSOLUTE path (`~` is fine — the launcher
expands it). The launcher is invoked by path from whatever directory the session
is in, so a relative home would resolve differently per caller and a `start`
from one project directory would not be visible to a `stop` from another. The
launcher refuses a relative value with a JSON error rather than splitting state.

## Workflow

### 1. Start the mirror

```bash
python3 "$SKILL_DIR/scripts/sim_mirror.py" start
```

Use the bundled launcher, **not** a bare `npx serve-sim`. It handles the parts
that silently break otherwise: detaching the server so it survives your turn
ending (a turn-scoped child gets reaped at turn end), device
selection/creation/boot, scoped stale-mirror cleanup, per-UDID pidfiles, and
refusing any non-loopback URL.

Optional `--device "<udid-or-name>"` targets a specific simulator (e.g. the one
your `xcodebuild` destination used). Without it: prefers an already-booted
device, else the newest iPhone, else creates one.

Prints JSON: `{"udid", "name", "url", "pid", "log"}`.

### 2. Open it in the Browser panel

Emit the hidden marker in your NEXT message, using the exact `url` from step 1:

```
<!-- kirocrew:preview url="http://127.0.0.1:PORT" -->
```

This is the only reliable auto-open path — a bare URL in prose merely pre-fills
the panel. Name the mirrored device in prose too.

**A loaded page is not proof the stream is healthy.** Confirm real frames are
arriving (see Failure modes) before telling the user it works.

### 3. Build, install, launch, drive

Normal simulator workflow against the SAME udid:

```bash
xcodebuild -scheme <Scheme> -destination "id=<udid>" build
xcrun simctl install <udid> <path/to/App.app>
xcrun simctl launch <udid> <bundle.id>
xcrun simctl io <udid> screenshot /tmp/shot.png   # your own verification
```

Everything you do shows up live in the user's panel. For your **own** checks
take `simctl` screenshots — the mirror is for the user; don't scrape it.

**Bound every `simctl` call with a timeout** (e.g. `subprocess.run(...,
timeout=90)`). A wedged CoreSimulator makes `simctl` block indefinitely, which
otherwise hangs your turn instead of returning a diagnosable error.

### 4. Stop when done

```bash
python3 "$SKILL_DIR/scripts/sim_mirror.py" stop                     # all tracked mirrors
python3 "$SKILL_DIR/scripts/sim_mirror.py" stop --shutdown-device   # also power off
python3 "$SKILL_DIR/scripts/sim_mirror.py" status                   # pid alive? url?
```

`stop` is scoped to this launcher's own pidfiles, and `--device` here must be a
**UDID** (not a device name) — it is validated before touching any path or
signaling anything. **Never** kill `serve-sim` processes you don't track —
another session may own them.

## Failure modes

Each of these was hit in practice; the stated cause is the verified one.

- **`simctl` missing** → Xcode not installed. Tell the user; stop.
- **"You have not agreed to the Xcode license agreements"** → the user must run
  `sudo xcodebuild -license accept` themselves (needs `sudo` + an interactive
  prompt you cannot drive).
- **"no available iOS runtimes"** → `xcodebuild -downloadPlatform iOS`
  (multi-GB; check free disk first).
- **`simctl` hangs, or repeated `error encoding frame: encodingFailed`** →
  CoreSimulator version mismatch. An Xcode upgrade **while a device was
  booted** orphans that device: the new framework cannot drive it, so `simctl`
  blocks and the mirror captures nothing while still serving HTTP 200.
  Recovery: stop the mirror, `pkill -x Simulator`, `pkill -f
  CoreSimulator.CoreSimulatorService`, `pkill -f launchd_sim`, then boot a
  fresh device. `launchctl remove` alone is not enough.
- **Device creation fails "Incompatible device"** → the global device-type list
  includes hardware the installed runtime rejects. The launcher already selects
  from the runtime's own `supportedDeviceTypes`; a hand-rolled `simctl create`
  is what trips this.
- **`serve-sim exited rc=…`** → read the printed log path. Usual causes: npx
  could not reach the public registry (proxy), or the udid was shut down
  externally. Fix and restart; do not loop more than twice.
- **Panel says the server stopped responding while the process is alive** →
  first confirm the URL is exactly the loopback one the launcher printed (never
  substitute a hostname). Requires a dashboard whose CSP admits loopback
  `connect-src`, since the panel's liveness probe is a `no-cors` fetch.

## Boundaries

- **View-only.** The user watches; whether taps forward at all depends on what
  `serve-sim`'s page itself offers. Never promise tap-through.
- Do not edit the user's `.xcodeproj`, schemes, or build settings to force a
  preview to work.
- One mirror per simulator; multiple simulators mean multiple `start` calls.
- Third-party dependency: `serve-sim` is fetched at runtime from public npm and
  is not vendored.
