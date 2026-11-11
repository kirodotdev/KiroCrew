# Agent Plugins

Owner: `kiro_crew.agent_plugins`. Tests: `test/test_agent_plugins_discovery.py`,
`test/test_agent_plugins_mcp.py`, and `test/test_agent_plugins_conformance.py`,
which pins the loader's verdict on the official conformance suite's own fixtures
(vendored in `test/fixtures/agent_plugins_conformance/`). Design:
[rfc-agent-plugins-standard.md](../../request-for-change/rfc-agent-plugins-standard.md).

Kiro Crew's client for the [Agent Plugins](https://agent-plugins.org/) 1.0.0
standard. This module discovers and validates plugins. It never activates one:
turning a plugin on and handing its skills and MCP servers to sessions are later
phases of the RFC, and until they land nothing calls this module at runtime.

## 1. Where plugins come from

The powers home is `~/.kiro/powers` (`kiro_home()` / `powers`), or the directory
`KIRO_POWERS_HOME` names. It is the tree Kiro IDE, kiro-cli and KAS share:

| Path | Meaning |
|---|---|
| `installed.json` | `{"installedPowers": [{"name": ...}]}`, the list of what is installed |
| `installed/<name>/` | One plugin root per listed name |
| `data/<name>/` | That plugin's `PLUGIN_DATA` directory |

`installed.json` is the list of record. A folder it does not name is not
installed, and a name that is not one plain path segment is dropped. A missing or
unreadable index lists nothing.

## 2. States

`discover_plugins()` returns one `PluginRecord` per listed name, sorted:

| State | When |
|---|---|
| `invalid` | The folder is missing, or `plugin.json` fails a fatal rule. No component of it is used |
| `not-a-plugin` | No `plugin.json`; a `POWER.md`-only power is named as such in `reason` |
| `off` | A valid plugin. Its skills, servers and per-component issues are filled in |

One plugin never hides another. A file that cannot be read or parsed, including
one nested too deeply to parse, fails only the component it belongs to: a bad
`plugin.json` makes the plugin `invalid`, a bad `mcp.json` turns MCP off, a bad
`SKILL.md` skips that skill, and a bad `installed.json` lists nothing. Any other
error while inspecting a plugin makes that plugin `invalid`, with a reason that
names only the error class, and the other plugins are still listed.

## 3. Rules applied

Written from the spec text, which wins over the published schemas. Schemas are
never fetched; only the 1.0.0 identifiers are recognised.

- **Manifest (§5).** `$schema` must be the 1.0.0 manifest identifier; another
  `agent-plugins.org` identifier is "unsupported version". `name` follows §5.5.
  Unknown top-level fields and a non-object `extensions` are reported and
  ignored; every other type violation (including an `author` field outside
  `name`/`email`/`url`) rejects the plugin. `extensions` values are kept
  unvalidated (§8.1).
- **Containment (§4.1).** Every package path is tested on its resolved target, so
  a link may point anywhere inside the plugin root and nowhere outside it.
- **Skills (§7.1).** Only immediate children of `skills/` holding a regular-file
  `SKILL.md` count. A skill needs YAML frontmatter (parsed with
  `NoAliasSafeLoader`) whose `name` follows the Agent Skills rule and equals its
  folder, and a non-empty `description` of at most 1024 characters. A bad skill
  is skipped; a `skills` that is not a folder turns skills off.
- **MCP (§7.2).** A document problem (bad JSON, an extra top-level field, a
  missing or mismatched `$schema`, a non-object `mcpServers`) turns MCP off. A bad
  entry is skipped:
  - `stdio`: closed fields; `command` is one token, bare or `./`-relative,
    never expanded, and a `./` command must resolve to a regular file inside the
    root (so a link spelled for another OS, such as a `C:/...` target read on
    POSIX, is skipped on every platform rather than kept as a missing path);
    optional `args`, `env`, and `cwd` fields must have their declared types when
    present (explicit `null` is invalid); `cwd` starts with `./`,
    `${PLUGIN_ROOT}` or `${PLUGIN_DATA}` and stays inside its base after
    resolution; `env` must not set `PLUGIN_ROOT` or `PLUGIN_DATA`
    (case-insensitively on Windows).
  - `streamable-http` / `sse`: closed fields; the optional `headers` field must
    have its declared object type when present (explicit `null` is invalid);
    absolute, syntactically valid `http(s)` URL with no whitespace, control
    characters, user information, or fragment, and with a valid hostname and
    port; plain `http` only for `localhost` or a loopback IP; header names are
    tokens, values hold no control characters, and no name repeats under
    different casing.
  - Any other `type`, including `http`, is unknown.

Valid entries are stored as written. Placeholder expansion needs the data
directory and belongs to the launcher.

## 4. Side effects

None. Discovery reads files only: it does not create `data/<name>/`, write any
index, start a process or add a skill root. Each read is size-bounded
(`plugin.json` and `mcp.json` 256 KiB, `SKILL.md` 1 MiB, `installed.json` 1 MiB).
