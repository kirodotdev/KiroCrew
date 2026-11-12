# Endpoint-security software blocking agent-spawned git/cr

**Symptom.** When the agent runs a command on your behalf — `git` (even a
read-only `git branch`), `cr`, or another tool — your endpoint-security product
(an "EDR", for example an injected behavioral sensor) kills it with a message
like:

> A process was blocked because malicious behavior was detected.

The same `git` and `cr` commands work when **you** type them in your own
terminal. Only the processes launched *by the agent* are blocked, so anything
that needs to spawn git — status, fetch, branch, commit, code review — fails.

This is distinct from the desktop-app renderer case covered in the
[Windows guide](windows-install.md) (where an injected DLL blocks the browser's
child processes); here it is the ordinary command-line tools the agent spawns
that are blocked.

## Why it happens on Windows

On the default backend (KAS), Kiro Crew does not start the agent's shell itself.
The shell (`pwsh.exe` or `powershell.exe`) is a child of the **`node.exe`** the
KAS engine runs under, so every shell tool call looks like "Node starts
PowerShell" to an endpoint product. Rules that block a script host starting
PowerShell (for example Kaspersky's Adaptive Anomaly Control) match that shape,
and the agent reports that the command could not run with `EPERM`. Releases
before KAS became the default ran the agent on Kiro CLI, which is why an
upgrade can be where the blocks start.

Which `node.exe` it is depends on the KAS route, which `kirocrew doctor` prints
on its `route:` row:

- **Relay** (kiro-cli is installed and signed in): the `node.exe` kiro-cli ships
  beside `kiro-cli.exe`, `%LOCALAPPDATA%\Kiro-Cli\node.exe` on a default kiro-cli
  install.
- **Bundle**: the `node.exe` of Kiro Crew's own engine, under
  `<data home>\engines\kas\<version>\node\`, or the copy staged in the desktop
  app's resources. `kirocrew doctor` prints its exact path on the `node:` row.

The detection entry's image path is the authority; the list above tells you
what to expect there. The interpreter-then-exec mechanism in the next section
does not apply on Windows.

## Why it happens on macOS and Linux

On macOS and Linux, for safety reasons Kiro Crew does not launch tools directly.
It starts each one through a tiny exec-shim — a short run of the Python
interpreter Kiro Crew itself runs under — that applies the child's process setup
(resource limits, working directory, controlling terminal) and then replaces
itself with the real command. The reason is a deadlock- and fd-leak class that
the obvious "configure the child, then run it" approach reintroduces on the
gateway's event loop; running the setup in a process that then execs the command
avoids it.

To a behavioral EDR, "an interpreter starts and immediately replaces itself with
another binary" matches a generic living-off-the-land / process-replacement
heuristic, so the whole process tree the agent spawns gets flagged — regardless
of the actual command. **It is a false positive:** the behavior is benign and
the spawned command is an ordinary `git`/`cr` invocation. No change to how the
tool is spawned reliably stops a behavioral sensor from flagging it, because the
sensor is reacting to the generic shape, not to anything specific; and the setup
the shim performs is a correctness requirement, not a toggle.

## The durable fix: a scoped exception

Add an **exception in your endpoint-security product scoped to the specific
detection and the exact image path that was blocked**, rather than a blanket
exclusion of everything the agent runs. A broad "exclude this app and every
process it launches" rule would take the sensor off every command the agent
spawns — and those are exactly the processes that act on untrusted external
content, so you do not want them unmonitored.

Security products key exceptions on the image path, so the exception must name
the real parent image, otherwise it will not match the process the rule flagged
and the blocks continue:

- **Windows:** the KAS engine's `node.exe` from
  [Why it happens on Windows](#why-it-happens-on-windows), paired with the
  "Node starts PowerShell" detection. It is not `KiroCrew.exe` and not Kiro
  Crew's Python.
- **macOS and Linux:** the process that starts and then replaces itself is **the
  Python interpreter Kiro Crew runs under**, not a `kirocrew` launcher: the path
  reported by `sys.executable` for the running gateway, typically the `python3`
  inside the install's virtual environment.

To build an accurate exception:

1. Open your security product's console (or the OS detection log) and read the
   detection entry: note the **detection id** that fired and the **exact image
   path** of the process it blocked or whose child it blocked.
2. Check that path against the image you expect. On Windows, run
   `kirocrew doctor` and compare with the `node.exe` for the route on its
   `route:` row. On macOS
   and Linux, run `python3 -c "import sys; print(sys.executable)"` from the same
   environment Kiro Crew runs in.
3. Hand the detection id and that image path to whoever administers endpoint
   security on the device, and ask for an exception scoped to that pair.

That ownership usually sits with an endpoint-security or IT team rather than with
the Kiro Crew user, because adding the exception requires policy access the user
does not have.

## Interim workaround

If you cannot get the exception added right away, run `git` and `cr` **from your
own terminal, outside the agent.** Those commands are not blocked when you run
them yourself, and doing so changes nothing about your Kiro Crew install, so it
is safe to leave in place for as long as you need. You lose the convenience of
the agent running them for you, but you are not blocked on migrating or working.

## What this is *not*

- **Not an SSH or credential problem.** A missing or removed SSH agent socket
  shows up as a `publickey`/permission-denied failure on `fetch` and `push`.
  `git branch` needs no SSH at all, so a sensor killing `git branch` is this
  behavioral-block issue, not an SSH one — do not wait on an SSH fix for it.
- **Not something a Kiro Crew setting can turn off.** On macOS and Linux there
  is deliberately no switch to make the agent spawn tools "unwrapped": the shim
  exists to prevent a gateway deadlock and a listening-socket fd leak, and
  removing it to appease a false positive would reintroduce both. The exception
  belongs in the security product.
