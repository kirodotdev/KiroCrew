# macOS Troubleshooting

Fixes for problems specific to macOS: the desktop app (`KiroCrew.app`) and the
gateway service that `kirocrew service install` sets up. For
install and build steps see [install.md](install.md); for problems that are not
macOS-specific, start with `kirocrew doctor`.

---

## A command works in Terminal but is "command not found" in the app

**Symptoms:** a CLI or MCP server binary runs fine in Terminal, but inside the
desktop app an agent shell tool or an MCP server spawn fails with
`command not found`. The binary is installed and executable; only the app
cannot find it.

### Why this happens

A GUI-launched `.app` does not start from your shell. It inherits launchd's
minimal environment, so its `PATH` is typically just:

```
/usr/bin:/bin:/usr/sbin:/sbin
```

The bundled Gateway and everything it spawns — agent shell tools, MCP servers,
ACP runtimes — inherit that value. The backend does re-add a fixed list of
well-known install locations (Apple Silicon Homebrew's `/opt/homebrew/bin`,
`~/.local/bin`, common version-manager shim directories), so CLIs there
resolve already. A CLI anywhere *outside* both the system `PATH` and that
fixed list — a custom `~/bin`, a tool-managed directory like
`~/.opencode/bin` — is unresolvable inside the app even though the same
command works in Terminal.

To recover your real `PATH`, the app reads the **launchd user domain**
(`launchctl getenv PATH`) just before it spawns the Gateway and appends the
directories found there. That domain is empty until something writes it: an
`export PATH=...` in `~/.zprofile` or `~/.zshrc` configures shells only and
never reaches launchd. That is why the general fix below is a `launchctl
setenv`, not another rc-file edit.

If **only an MCP server launcher** is missing, prefer the narrower
`mcp.extra_path_dirs` config list. It extends MCP resolution and the MCP gateway
daemon without changing GUI applications' general `PATH`; entries must be
absolute, and a gateway restart is required because the daemon inherits its
`PATH` at spawn. It does not affect ordinary agent shell commands. When those are
missing too, either add the directories as a `PATH=` line in `~/.kiro/crew/.env`
([Credentials](../../src/kiro_crew/docs/configuration.md#credentials)), which is
appended behind the app's `PATH`, or use the launchd fix below.

### The fix

1. From a Terminal window whose `PATH` is correct (one where the command
   resolves), copy that `PATH` into the launchd user domain:

   ```bash
   launchctl setenv PATH "$PATH"
   ```

   Note the scope: the launchd user domain is shared, so every GUI app
   launchd starts for your user afterwards inherits this `PATH` — not just
   Kiro Crew. You are copying the same value your shells already use, but if
   you prefer to keep the change minimal, set a `PATH` that starts with the
   system directories and appends only the directories you need.

2. **Fully quit and relaunch the app** — quit from the menu bar or with Cmd+Q,
   not just the window close button. The Gateway keeps the environment it was
   started with, and so do its MCP server and agent children, so nothing picks
   up the new value until the app respawns the Gateway.

To verify: `launchctl getenv PATH` in Terminal prints the full value, and the
command now resolves inside the app (an agent shell tool running
`command -v <name>` prints its path).

### The setting does not survive a reboot

`launchctl setenv` writes in-memory launchd state, so after a reboot or logout
the domain is empty again and the app is back to the minimal `PATH` until the
command is re-run. Either re-run it when the symptom reappears, or automate it
with a LaunchAgent that runs at login. Save the following as
`~/Library/LaunchAgents/dev.kirocrew.path.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.kirocrew.path</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/launchctl</string>
    <string>setenv</string>
    <string>PATH</string>
    <string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
```

A LaunchAgent does not run your shell rc files, so `$PATH` is not available
inside it: write the literal value (print yours with `echo "$PATH"`) into the
last `<string>`. Keep the system directories first and append your own, as in
the example. If a directory name contains an XML-reserved character, escape
it (`&` as `&amp;`, `<` as `&lt;`) — a raw `&` makes the file unparseable and
the agent silently fails to load. Load it once with
`launchctl load ~/Library/LaunchAgents/dev.kirocrew.path.plist`; from then on
it runs at every login. If the app auto-starts at login, launch order is not
guaranteed on the first login after adding the agent — quit and relaunch the
app once if a command is still missing.

### What the app does with the launchd `PATH`

- Directories read from the launchd domain are **appended after** the app's
  inherited `PATH`. Inside Kiro Crew they can make a name resolve that
  resolved nowhere before, but can never shadow a system binary that already
  resolves. (This guarantee is about the app's own merge — the
  `launchctl setenv` step above is governed by whatever ordering you set.)
- Only absolute entries are added: a relative entry, or one containing a `..`
  segment, is ignored.
- A Gateway started from a terminal (`kirocrew gateway`) is unaffected — it
  inherits the shell's `PATH` directly and needs none of this.

---

## The gateway does not start after a reboot until someone logs in

**Symptoms:** on a Mac with automatic login off (a headless Mac mini reached
over SSH or Tailscale, say), the gateway is down after a reboot. Nothing
answers on the gateway port (5476 by default) until someone logs in at the Mac's login window. Then it
comes up on its own.

This is expected. `kirocrew service install` sets up a **per-user
LaunchAgent** (`~/Library/LaunchAgents/dev.kirocrew.gateway.plist`), not a
system daemon. launchd loads it into your login session, the `gui/<uid>`
domain, and that domain exists only once you log in at the Mac itself. An SSH
login does not create it. Running in your login session means the gateway
sees what your Terminal sees, such as the login Keychain, which is normally
unlocked only after a login at the Mac. Before that login, it does not run.

To check from SSH:

```bash
launchctl print "gui/$(id -u)/dev.kirocrew.gateway"
lsof -nP -iTCP:5476 -sTCP:LISTEN
```

Before anyone logs in, the first command says it cannot find the `gui`
domain, and the second prints nothing. After a login, the first shows the job
with a `pid`, and the second shows the gateway listening. If your gateway uses
another port (set by `dashboard.url` in the config or by `KIROCREW_PORT`), put
yours in place of 5476. If you ran `kirocrew service install` over SSH while nobody
was logged in, the job went into `user/<uid>` for that boot instead; check it
with `launchctl print "user/$(id -u)/dev.kirocrew.gateway"`.

If you need the gateway up after every reboot, turn on automatic login for
that user (System Settings > Users & Groups), or log in once after each
reboot.

**A `gateway.lock` left over from the last boot is safe.** The file
`~/.kiro/crew/gateway.lock` (or `$KIROCREW_HOME/gateway.lock`) keeps the pid of
the gateway that last ran, but the lock itself is held by a running process,
and a reboot drops it. The next gateway takes the file over and writes its own
pid. Do not delete it. If a start is refused because the lock is held, some
process really holds it, usually a running gateway, sometimes a leftover child
of one. `lsof ~/.kiro/crew/gateway.lock` names it, and the refusal message says
which case it found.

---

## The agent's git/cr commands are killed by security software

**Symptoms:** the agent's `git` or `cr` commands (even a read-only
`git branch`) are blocked with "A process was blocked because malicious behavior
was detected" from your endpoint-security product, while the same commands run
fine when you type them in Terminal yourself.

This is not macOS-specific — it affects any OS with a behavioral sensor — so it
has its own guide:
[endpoint-security-blocking-spawned-tools.md](endpoint-security-blocking-spawned-tools.md).
In short: it is a false positive on the way the agent spawns tools, the durable
fix is an exception scoped to the specific detection id and the exact image path
that was blocked — the Python interpreter Kiro Crew runs under (owned by whoever
administers endpoint security on the device) — and the interim workaround is to
run `git`/`cr` from your own terminal.
