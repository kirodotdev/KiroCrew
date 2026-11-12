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
3. **The desktop session check**, with the open-source driver on Windows and
   Linux. On macOS the built-in driver needs no setup step; its two privacy
   permissions show on the Setup card (see [macOS](#macos)).
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

Kiro Crew has two drivers: the **built-in driver** (its own, on macOS and
Windows) and the **open-source driver** (the MIT-licensed cua-driver project). On
Linux it uses the open-source driver. On Windows it uses the open-source driver
once you install it from the setup dialog, and the built-in driver until then. On
macOS Kiro Crew uses the built-in driver; the open-source driver is not offered on macOS yet, and choosing it there
shows as not supported. The open-source driver is not bundled: the install
downloads one pinned release from the project's official GitHub releases, and
Kiro Crew checks its size, checksum and signature before it
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

The grant belongs to the app that started Kiro Crew (for example the Kiro Crew
app, or Terminal when you started it from a shell), so that is the entry to turn
on. The Setup card shows what Kiro Crew can read about both permissions and
names that app. The reading is advisory: macOS does not always report a grant
given to the app that started Kiro Crew, so a "not detected" reading never
blocks Computer Use on its own. When a permission reads as missing, the card has
an **Open … Settings** button for it that opens the right pane once per click.
Kiro Crew never changes these settings for you.

macOS delivers a click to the target application alone, so your own pointer does
not move and you can keep working while the agent drives another window. One
route deliberately warps the real cursor, for a click that has to be physically
real. The agent has to name that route explicitly, it is never chosen
automatically, and it should warn you first.

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

**I turned the permission on, but the Mac card still says it is missing.**
macOS applies a new permission only to a freshly started process: quit and
reopen the app the card names (the one that started Kiro Crew). Make sure the
switch you turned on is for that app.

**macOS says the open-source driver is not supported.** It is not offered on
macOS yet. Choose **Use the built-in driver** in the setup dialog, or set the
driver to **auto** or **native**.

**The driver is not answering.** Choose **Restart driver** on the dialog or the
Setup card. If it keeps happening, include the **Diagnostics** list when you
report it.

**The driver download failed.** Nothing was installed. A network that blocks
GitHub downloads stops the install: allow `github.com` and
`objects.githubusercontent.com`, then choose **Try the install again**.

**Setup says the driver was stopped as soon as it started.** The download and
every check passed, but the system stopped the driver the moment it ran (on
Linux this shows as "signal 9", on Windows as "access denied" or "blocked by
policy"). On a managed computer this is usually endpoint security software that
has not been told to allow it. Ask your IT team to allow the open-source driver. Until then,
on Windows keep the driver on **auto** (or **native**): Computer Use keeps
working on the built-in driver, and the Settings card says so. Kiro Crew tries the open-source driver once per install or
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
