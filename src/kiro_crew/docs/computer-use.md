# Computer use

Computer use lets the agent read and drive your real desktop applications — the
work that does not live in a web page. It reads a window as a structured outline
of its controls, then clicks, types, sets values, scrolls, and drags.

It is **off by default** and you turn it on in **Settings → Computer Use**.
Granting it means granting full desktop observation plus the ability to
synthesize input, so treat the switch as a security ceiling rather than a
preference.

The enable does not live in `config.json`. It sits in its own protected file
precisely because `config.json` is writable by the agent's own shell, so an
enable stored there could be flipped by a prompt injection rather than by you.

## Turning it on

Open **Settings → Computer Use** and choose **Set up Computer Use**. A short
setup dialog walks you through what this computer needs, one step at a time:

1. **Give Kiro Crew access to your computer.** Continue turns the feature on.
   Your open chats restart so they pick up the new tools; your messages stay.
   Not now closes the dialog and changes nothing.
2. **Install the driver**, when this computer uses the open-source driver (see
   [The driver](#the-driver)).
3. **The platform's own step**: two privacy permissions on macOS, a desktop
   session check on Windows and Linux.
4. **Verify.** Kiro Crew lists your windows, reads one window's controls and
   takes one picture. The check never clicks or types.
5. **Ready.** The dialog says Ready only after the check passed.

The dialog keeps its place on the gateway, so you can close it at any point and
pick up where you left off. If an agent's computer-use call was refused because
setup is not finished, the chat shows a **Set up Computer Use** button under that
call, which opens the same dialog.

The **Setup** card on the same Settings page shows the state at a glance: whether
Computer Use is on, the driver and its version, each permission, the desktop
session, and anything that works differently on this computer. It offers one
button for whatever is missing, **Restart driver** when the driver is installed,
and a folded **Diagnostics** list to include when you report a problem.

To turn Computer Use off again, use the **Enable Computer Use** switch at the top
of the page. Nothing else turns it on or off, and an agent cannot.

## The driver

On Linux Kiro Crew drives apps through the open-source **cua-driver** (MIT
licensed). On Windows it uses that driver once you install it from the setup
dialog, and its own Windows driver until then. On macOS Kiro Crew's own driver
stays the default and the open-source one can be chosen. The driver is not
bundled: the install downloads one pinned release from the project's official
GitHub releases, and Kiro Crew checks its size, checksum and signature before it
runs anything. A file that does not match is never run, and the check repeats
every time the driver starts. The driver's telemetry and update checks are
turned off, and the agent never talks to the driver directly: every call still
goes through Kiro Crew's own checks first.

## macOS

macOS asks for two permissions in **System Settings → Privacy & Security**:

| Permission | What it is for |
|---|---|
| **Accessibility** | Reading the buttons and fields of an app window, and clicking or typing in them. |
| **Screen Recording** (Screen & System Audio Recording) | Taking a picture of the one window the agent is working in, to check what happened. |

With the open-source driver, the entry you turn on is called **CuaDriver**, not
Kiro Crew. That is the driver's own signed app, and macOS grants permissions to
the app that asks for them. The setup dialog names the exact entry to look for.
Because the grant belongs to CuaDriver, it survives Kiro Crew updates.

Each permission step has an **Open … Settings** button that opens the right pane
once per click, and a **Check again** button. After you turn a switch on, the
dialog notices by itself for a couple of minutes. Kiro Crew never changes these
settings for you.

macOS delivers a click to the target application alone, so your own pointer does
not move and you can keep working while the agent drives another window. One
route deliberately warps the real cursor, for a click that has to be physically
real. The agent has to name that route explicitly, it is never chosen
automatically, and it should warn you first.

### The open-source driver on macOS (preview)

On a Mac, Kiro Crew's own driver is the default. The open-source driver is a
**preview** there: its download, signature checks and refusals are proven, but
granting the permissions and real control of apps have not been proven yet. The
Setup card marks it **Preview**.

When you install it, the driver app is placed at `/Applications/CuaDriver.app`.
That is the only place macOS lets it start from, and where Privacy & Security
lists it. The setup dialog says so before you click **Install driver**.

- If a `CuaDriver.app` is already there, Kiro Crew uses it only when it is
  exactly the pinned version with the expected signature. Otherwise the install
  stops with "another version of CuaDriver is installed"; remove or update that
  copy yourself, then install again. Kiro Crew never replaces or removes a copy
  it did not install.
- Every time the driver starts, Kiro Crew checks the copy in `/Applications`
  again (signature, team, bundle id, checksum). A copy that fails is never
  started. Kiro Crew never runs the program inside `/Applications` directly: it
  is only opened by macOS, and every other driver command runs a second, sealed
  copy in Kiro Crew's data folder that is checked against its checksum each time.
- If your account cannot write to `/Applications`, the install says so. Kiro
  Crew never asks for an administrator password.
- **Reset setup** removes the driver Kiro Crew installed. Its copy in
  `/Applications` is removed only when Kiro Crew placed it at that path and the
  driver binary inside it is unchanged; other files in the bundle are not
  compared.
- Kiro Crew starts the driver's background service itself, with telemetry and
  update checks off and a private home inside Kiro Crew's data folder, on a
  private socket that no other tool uses. Before using it, Kiro Crew checks how
  macOS started that process; if it cannot prove those settings, it stops the
  service and refuses. It never attaches to the driver's shared per-user
  service, and it stops only the service it started. This needs a macOS whose
  `open` command supports `--env` (macOS 26 does); on an older macOS the
  open-source driver refuses to start.
- The driver's own startup permission check is turned off
  (`--no-permissions-gate`), so it never opens System Settings by itself. The
  only prompts are the ones you trigger in the setup dialog: **Allow** (the
  driver's guided grant, once per click) and the two **Open … Settings**
  buttons. A missing permission still shows as a setup step and makes the tools
  refuse with the reason.
- Not proven yet on a signed-in Mac: whether the permissions you grant to
  CuaDriver apply to this privately started service with that check off. Step 6
  below settles it.

#### How to check it on your Mac

About 15 minutes, signed in at your Mac's desktop, in an admin account, with no
`/Applications/CuaDriver.app` already present (if one is, note its version and
stop: install must refuse it unless it is exactly the pinned version). Use a
throwaway data home so your real setup is untouched:

1. From a source checkout of this version, create a virtual environment and
   install Kiro Crew into it, then
   `export KIROCREW_HOME=/tmp/kc-cu-home KIRO_HOME=/tmp/kc-cu-kiro`. Keep your
   real `HOME` (macOS permissions are per user).
2. Choose the open-source driver and turn the feature on in that home:
   `python -c "from kiro_crew.computer_use import enable_state as e; s=e.load_state(); s.update(enabled=True, driver='upstream'); e.save_state(s)"`
3. In a second terminal, watch the network:
   `sudo tcpdump -i pktap,all -k NP -n 'udp port 53 or tcp port 443' | grep -i -E 'cua|posthog'`
4. Install: `python -c "import time; from kiro_crew.computer_use import setup as s; s.run_setup_action('install'); time.sleep(30); print(s.get_setup_status()['state'])"`.
   Expect `requesting_accessibility`, `/Applications/CuaDriver.app` present,
   and `"owned": true` in `/tmp/kc-cu-home/computer-use/install.json`.
5. Start it with `run_setup_action('recheck')`, then check the service:
   `pgrep -fl CuaDriver` must show `serve --socket` with a path under
   `/tmp/kc-cu-home/computer-use-ipc/run/`; `ps -E -p <pid>` must show
   `CUA_DRIVER_RS_TELEMETRY_ENABLED=0`, `DO_NOT_TRACK=1` and
   `CUA_DRIVER_RS_HOME=/tmp/kc-cu-home/computer-use/home`; `~/.cua-driver`
   must not appear; and the tcpdump terminal must stay silent.
6. Ask for the permissions with `run_setup_action('request_permissions')`. Note
   whether a prompt appears and what name Privacy & Security shows (expect
   **CuaDriver**). Turn on Accessibility and Screen & System Audio Recording.
7. Run `recheck`, then `restart_driver`, then `verify`, printing the state after
   each. Expect `ready` at the end; this is the step that shows whether the
   grants apply to the private service.
8. Open TextEdit with a new document and run
   `kirocrew computer call --calls '[{"tool":"computer_get_state","args":{"app":"TextEdit"}}]'`.
   Take the text area's index N, then type into it with `computer_type_text`
   (`element_index` N, `text` "hello") in the same `--calls` list. Expect "hello"
   in the document and in the refreshed tree. A password field (a login page or
   Users settings) must show as `<secure>`, and typing into it must be refused.
9. Turn Accessibility off for CuaDriver, run `recheck`, and expect
   `permission_revoked`.
10. Clean up: `run_setup_action('reset')`. Expect `/Applications/CuaDriver.app`
    gone and `pgrep -fl CuaDriver` empty. Then
    `tccutil reset Accessibility com.trycua.driver`,
    `tccutil reset ScreenCapture com.trycua.driver`, and delete
    `/tmp/kc-cu-home` and `/tmp/kc-cu-kiro`.

Report the state after each step, the name Privacy & Security showed, whether
typing landed, and the `pgrep`, `ps -E` and tcpdump lines from step 5.

## Windows

Windows has no permission screen for this. Setup installs the driver when needed,
checks that Kiro Crew runs in your signed-in desktop session, then verifies.

With the open-source driver, clicks, typing and scrolling are delivered to the
window in the background, so your pointer does not move. Some windows (browsers,
some toolkits) refuse background input; the agent then has to name the route that
brings the window forward, and it should tell you first. Kiro Crew's own Windows
driver has no per-process input delivery: a keystroke takes your keyboard focus
and a coordinate click **moves your real cursor**, and the agent is required to
tell you rather than succeed silently.

## Linux

Linux uses the open-source driver. Setup installs it, then detects your desktop:
an X11 session, or a Wayland desktop with XWayland, plus the accessibility
(AT-SPI) bus that apps use to describe their controls. On Wayland only apps that
run under XWayland are visible; native Wayland windows are not listed or
captured, and a Wayland session with no X server at all is reported as not
supported.

Clicking a control, typing into a field and setting a value happen in the
background. Background key presses, scrolling and coordinate clicks or drags
need a virtual input device (write access to `/dev/uinput`); without it they are
refused by name rather than faked, and the agent can bring the window forward for
a click or drag instead. The Setup card lists what is not available on your
desktop.

If the driver cannot be used at all, every computer-use tool returns a "not
supported on this platform" refusal that says why.

## Troubleshooting

**I turned the permission on, but setup still says it is missing.** Choose
**Check again**. macOS applies a new permission only to a freshly started
process, so the dialog may ask you to **Restart driver**; that restarts the
driver only, never Kiro Crew or your chats. Make sure the switch you turned on is
the entry the dialog names (CuaDriver for the open-source driver).

**Setup says a permission was turned off.** Something removed a grant you gave
earlier (you, an administrator, or a macOS update). Use **Open … Settings** to
turn it back on, then **Check again**.

**The driver is not answering.** Choose **Restart driver** on the dialog or the
Setup card. If it keeps happening, include the **Diagnostics** list when you
report it.

**The driver download failed.** Nothing was installed. A network that blocks
GitHub downloads stops the install: allow `github.com` and
`objects.githubusercontent.com`, then choose **Try the install again**.

**Setup says the driver was stopped as soon as it started.** The download and
every check passed, but the system stopped the driver the moment it ran (on a
Mac this shows as "signal 9"). On a managed computer this is usually endpoint
security software that has not been told to allow it. Ask your IT team to allow
CuaDriver. Until then, on macOS and Windows keep the driver on **auto** (or
**native**): Computer Use keeps working on the built-in driver, and the Settings
card says so. Kiro Crew tries the open-source driver once per install or
**Restart driver**, and never tries to get around the block.

**Windows says there is no desktop to control (Session 0).** Kiro Crew is
running as a background service, which has no desktop. Start Kiro Crew from your
signed-in Windows desktop instead, then choose **Check again**.

**Linux says no desktop session was found.** Start Kiro Crew from inside your
graphical session, so it sees `DISPLAY` or `WAYLAND_DISPLAY` and the
accessibility bus. A gateway started over SSH or from a system service has
neither.

**Setup keeps failing.** **Reset setup** removes the driver Kiro Crew installed
and starts the walk over. It does not remove any permission you granted.

## What is off limits

- **Password fields are redacted.** They render as `<secure>` in the outline, and
  a window containing one is not captured as an image.
- **Kiro Crew's own dashboard is refused**, for reading as well as typing.
  Driving it would let the agent change its own security settings, including this
  one. On macOS the refusal covers every window of the dashboard's process, not
  just the visible one, and the refusal names the target so it is clear why.

## How it drives a window

The agent reads the window first and prefers to address a control by its position
in that outline rather than by pixel coordinates. That is what makes the
protections above enforceable: a password field is refused by its identity in the
tree, which pixels cannot express. Coordinates remain available for canvases,
sliders, and custom-drawn interfaces that expose no usable control.

## Related docs

- [Configuration](configuration.md): the config file and what is deliberately not in it
- [Blocked commands](blocked-commands.md): the other refusals and how to read them
- [Dashboard](dashboard.md): Settings and where the tabs live
