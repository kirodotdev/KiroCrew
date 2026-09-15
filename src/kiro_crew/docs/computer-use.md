# Computer use

Computer use lets the agent read and drive your real desktop applications — the
work that does not live in a web page. It reads a window as a structured outline
of its controls, then clicks, types, sets values, scrolls, and drags.

It is **off by default** and you turn it on in **Settings → Computer Use**.
Granting it means granting full desktop observation plus the ability to
synthesize input, so treat the switch as a security ceiling rather than a
preference.

The enable does not live in `config.json`. It sits in its own protected file
precisely because `config.json` is writable by the agent's own shell — an enable
stored there could be flipped by a prompt injection rather than by you.

## macOS and Windows

Both platforms support the full tool set. They differ in one way that changes
what you see:

- **macOS** delivers a click to the target application alone, so your own pointer
  does not move. You can keep working while the agent drives another window.
- **Windows** has no per-process input delivery. A keystroke takes your keyboard
  focus and a coordinate click **moves your real cursor**. The agent is required
  to tell you rather than succeed silently.

On macOS there is also one route that deliberately warps the real cursor, for a
click that has to be physically real. The agent has to name that route
explicitly — it is never chosen automatically — and should warn you first,
because your pointer will jump out from under your hand.

## What is off limits

- **Password fields are redacted.** They render as `<secure>` in the outline, and
  a window containing one is not captured as an image.
- **Kiro Crew's own dashboard is refused** — for reading as well as typing.
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
