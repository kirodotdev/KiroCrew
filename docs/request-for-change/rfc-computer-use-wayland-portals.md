---
title: Computer use on Wayland -- capture and input through the desktop portals
status: draft
author: lummi-ch23
created: 2026-10-28
last-audited: 2026-10-28
audited-at: 3ae7e876
doc-pr: 25230
implementation-prs: []
tracking-issues: [25221]
supersedes: []
superseded-by: []
---

# RFC: Computer use on Wayland -- capture and input through the desktop portals

The Linux driver proposed in [#25222](https://github.com/kirodotdev/KiroCrew/pull/25222)
drives the full tool set on X11 and a tree-only subset on Wayland: AT-SPI is a D-Bus
protocol and works on both, but Wayland gives an unprivileged client no window
capture and no synthetic input. That covers cloud workstations and XFCE/MATE
desktops, and leaves out the default desktop of every current GNOME and KDE
distribution. This document proposes closing the gap through the two portals the
compositors already ship for exactly this, `org.freedesktop.portal.ScreenCast` and
`org.freedesktop.portal.RemoteDesktop`, and records the one decision that is
expensive to reverse once shipped: **consent is per application window, granted in
the compositor's own dialog, and kept as a restore token the gateway stores.**

## 1. Problem

On a Wayland desktop with #25222 applied, a model can read every window's tree and
act on any element by index, but:

- `computer_get_state` returns no screenshot, so canvases, charts and anything an
  app does not expose to AT-SPI are invisible;
- `computer_press_key` refuses, so Return, Escape, shortcuts and menu accelerators
  are unavailable;
- `computer_click` / `computer_drag` with coordinates refuse, so a map, a canvas or
  a custom-drawn control cannot be operated;
- `computer_type_text` works only into editable text, never into a terminal or a
  canvas-based editor.

These are deliberate Wayland properties, not bugs: the compositor alone owns pixels
and input. The supported way for a client to get either is a portal, which asks the
person at the desktop.

## 2. Goals and non-goals

Goals:

- Screenshots, keystrokes and pointer gestures on GNOME (Mutter) and KDE Plasma 6
  Wayland sessions, behind a consent the person at the desktop gives once per
  window, with the same chokepoint, policy floors and secure-field rules as X11.
- No new compiled dependency in the gateway for the D-Bus side (`jeepney` already
  carries it).
- A refusal that names the portal and the step the operator must take whenever a
  consent is missing, revoked or unsupported.

Non-goals:

- wlroots compositors (Sway, Hyprland). `xdg-desktop-portal-wlr` has ScreenCast but
  no RemoteDesktop; their virtual-pointer and virtual-keyboard protocols are
  privileged and compositor-specific. They stay tree-only.
- Headless use. A portal dialog needs a person; a gateway with nobody at the
  desktop stays tree-only on Wayland, which is what X11 virtual displays are for.
- Any change to the tool contract. `want_image` is already a request, refusals are
  already typed text.

## 3. Design

### 3.1 One portal session per gateway, opened on first need

The driver opens a combined RemoteDesktop session (RemoteDesktop `CreateSession`,
`SelectDevices(types=keyboard|pointer, persist_mode=2)`, ScreenCast
`SelectSources(types=WINDOW, multiple=true, persist_mode=2)`, `Start`) the first time
a verb needs capture or input, never at startup. Every portal call is a
Request/Response pair over D-Bus; `jeepney` handles the signal subscription the same
way `linux_atspi.py` already batches calls.

`Start` shows the compositor's dialog. The person picks the window(s) and allows
remote control. The verb that triggered it waits for the Response with a bound
(proposed 120 s) and refuses with "approve the screen-sharing request on your desktop"
when it times out; nothing proceeds on an unanswered dialog.

### 3.2 Restore tokens are the consent record

`persist_mode=2` returns a `restore_token` that reopens the same grant without a
dialog. It is a credential: whoever holds it controls the desktop within that grant.
It is stored under the gateway's data dir with mode 0600, never in config, never
logged, and cleared when the operator turns computer use off. Revocation is the
compositor's own (GNOME Settings > Privacy > Screen sharing, KDE the same), and a
rejected token falls back to a fresh dialog, never to silence.

The agent must not be able to read it. Mode 0600 alone does not achieve that: an
agent-spawned shell runs as the same uid as the gateway, so it could read the token
and open its own portal session with it, outside the computer-use chokepoint (no
policy, no secure floor, no audit). The token therefore lives in its own directory
listed in `_CREW_HIDDEN_LEAVES` (`sandbox_leaves.py`), so the sandbox mask hides it
from every agent-spawned process, the same treatment `diag` and `.env` get. Only the
gateway process, which runs the driver, reads it. Where no sandbox mask is in force
(an unsandboxed session), the token is no better protected than any other gateway
credential on disk; that is stated in the doctor output rather than hidden.

### 3.3 Windows, not monitors

`types=WINDOW` keeps the X11 contract: a snapshot shows one application's window,
not the screen, so a password field in another window cannot leak into a picture.
Each stream carries the compositor's window id and size; the driver maps a stream to
the AT-SPI frame by application id and title (the same matching `_find_window_for`
does for launches) and refuses to attach an image when the mapping is ambiguous.

### 3.4 Capture: one PipeWire frame on demand

A ScreenCast stream is a PipeWire node handed over as a file descriptor
(`OpenPipeWireRemote`). The driver needs one frame per snapshot, not a video. Two
options, to be settled by the Phase 2 spike:

- **ctypes over `libpipewire-0.3`**: connect a stream to the node, accept the first
  `SPA_DATA_MemFd` / `MemPtr` buffer (refusing DMA-BUF unless a CPU-mappable format
  is negotiated), copy, disconnect. Stdlib-only in Python, but SPA pod construction
  is intricate and the ABI is C-macro-heavy.
- **A small helper process** that does the same and prints one PPM frame. Simpler
  code, but it is a spawn inside the computer-use package and would need its own
  audited exemption, argv pinned like the launch spawns.

The proposal prefers the first; the spike decides.

### 3.5 Input: RemoteDesktop notifications

- Keys: `NotifyKeyboardKeysym`, so Unicode typing needs no keymap tricks. Focus is
  verified exactly as on X11, through AT-SPI (`focused` on the element, `active` on
  its frame), before every keystroke; an unverified focus refuses.
- Pointer: `NotifyPointerMotionAbsolute(stream, x, y)` in the coordinate space of a
  WINDOW stream, then `NotifyPointerButton` / `NotifyPointerAxis`. Coordinates are
  therefore window-relative, which matches the window-space extents the tree-only
  mode already reports, and a point outside the stream's size is refused before it
  is sent. The pointer route still requires `click_method: "global"` by name: the
  person's real cursor moves.
- Whether Mutter accepts absolute motion on a window stream (not only a monitor
  stream) is unverified; it is the first thing the Phase 1 spike checks. If it does
  not, coordinate gestures on GNOME stay refused rather than falling back to a
  monitor stream that would widen the grant.

### 3.6 What does not change

The chokepoint, the keystone enable, the index and drift check, the secure-field
floor (a window with a `password text` node still gets no screenshot), and the
tree-only behaviour when no portal session exists. A portal session is a capability
the driver may use, never a reason to skip a check.

## 4. Alternatives considered

- **Screenshot portal** (`org.freedesktop.portal.Screenshot`): one call, a PNG file,
  silent after the first grant on GNOME. Rejected as the primary route because it
  captures the whole monitor and the driver cannot know where the window is to crop
  it, so another window's secret can enter the image.
- **libei / `ConnectToEIS`** for input: newer and lower-latency, but needs a C
  library binding and portal 1.18+. A later swap behind the same verbs, not a
  prerequisite.
- **Compositor-private D-Bus** (GNOME Shell `Eval`, KWin scripting): disabled or
  unstable across releases and bypasses consent. Rejected.
- **Run apps under Xwayland**: works for X clients only, and native Wayland apps are
  most of a modern desktop.

## 5. Rollout

1. Spike on GNOME 46+ and Plasma 6: RemoteDesktop with a WINDOW stream, absolute
   motion on it, restore-token reuse. Outcome recorded here before any code.
2. Keyboard and pointer through RemoteDesktop (§3.5), with the consent and token
   handling (§3.1, §3.2). Screenshots stay off.
3. Capture (§3.4) after its spike, with the window mapping (§3.3).
4. Settings shows the session kind and whether a portal grant is held, with a
   button to forget it.

## 6. Open questions

1. Is a 120 s bound on the consent dialog right, or should the first verb refuse
   immediately and tell the model to retry once the person has approved?
2. Should the restore token be scoped per crew member, or one per gateway?
3. ctypes PipeWire or an audited helper process for the frame grab (§3.4)?
