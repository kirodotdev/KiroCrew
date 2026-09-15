# macOS Troubleshooting

Fixes for problems specific to the macOS desktop app (`KiroCrew.app`). For
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
fixed list — Intel Homebrew's `/usr/local/bin`, a custom `~/bin`, a
tool-managed directory like `~/.opencode/bin` — is unresolvable inside the
app even though the same command works in Terminal.

To recover your real `PATH`, the app reads the **launchd user domain**
(`launchctl getenv PATH`) just before it spawns the Gateway and appends the
directories found there. That domain is empty until something writes it: an
`export PATH=...` in `~/.zprofile` or `~/.zshrc` configures shells only and
never reaches launchd. That is why the fix below is a `launchctl setenv`, not
another rc-file edit.

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
