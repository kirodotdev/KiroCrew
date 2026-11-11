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

On Linux, and on macOS or Windows when it is selected, Kiro Crew drives apps
through the open-source **cua-driver** (MIT licensed). It is not bundled: the
first install downloads one pinned release from the project's official GitHub
releases, and Kiro Crew checks its size, checksum and signature before it runs
anything. A file that does not match is never run. The driver's telemetry and
update checks are turned off, and the agent never talks to the driver directly:
every call still goes through Kiro Crew's own checks first.

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

## Windows

Windows has no permission screen for this. Setup installs the driver when needed,
checks that Kiro Crew runs in your signed-in desktop session, then verifies.

Windows has no per-process input delivery. A keystroke takes your keyboard focus
and a coordinate click **moves your real cursor**. The agent is required to tell
you rather than succeed silently.

## Linux

Without a desktop driver, every computer-use tool returns a "not
supported on this platform" refusal, and **Settings → Computer Use** shows the
reason instead of a switch, so it cannot be turned on there.

With the open-source driver, setup installs it, then detects your desktop: an
X11 session, or a Wayland compositor, plus the accessibility (AT-SPI) bus that
apps use to describe their controls. On Wayland your desktop may ask you to
approve screen sharing the first time the agent takes a picture; choose Allow.
Some compositors do not let any app move the pointer or read other windows.
Those actions are refused by name rather than faked, and the Setup card lists
what is not available on your desktop.

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

**Windows says there is no desktop to control (Session 0).** Kiro Crew is
running as a background service, which has no desktop. Start Kiro Crew from your
signed-in Windows desktop instead, then choose **Check again**.

**Linux says no desktop session was found.** Start Kiro Crew from inside your
graphical session, so it sees `DISPLAY` or `WAYLAND_DISPLAY` and the
accessibility bus. A gateway started over SSH or from a system service has
neither.

**Setup keeps failing.** **Reset setup** starts the walk over. It does not
remove any permission you granted.

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
