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

## 4. Converting client-specific plugins

`agent_plugins.convert.convert_plugin(src, dest)` turns a Claude Code, Codex or
Cursor package into a standard plugin, following the spec's migration guide. No
caller uses it yet; the install flow (RFC Phase 4) will. Tests:
`test/test_agent_plugins_convert.py`.

- **Input.** The first of `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`,
  `.cursor-plugin/plugin.json`, `.plugin/plugin.json`, `.github/plugin/plugin.json`,
  else a root `plugin.json` without an Agent Plugins `$schema`. A folder that is
  already standard, or has none of these, is refused. Every file the converter
  reads from the package (manifests, MCP files, `SKILL.md`) is reached through
  no link and must resolve inside the source; a manifest behind a linked folder
  is never chosen.
- **Output.** A copy of every regular file of the package (symbolic links and
  Windows directory junctions are neither followed nor copied, `.git` and
  `node_modules` are skipped, 20,000 files and 200 MiB at most), plus a generated
  `plugin.json`, `mcp.json` and `skills/`. Generated JSON is indented when it fits
  the loader's 256 KiB component limit and compact otherwise; a component that
  still exceeds the limit is refused. The client's own files stay beside them. A
  `dest` that exists or resolves inside the source is refused before anything is
  created. The output is built in a hidden staging folder next to `dest`, validated
  with the loader, and moved into place only when the manifest is valid. On any
  failure the staging folder, the skills temporary folder and any parent folders
  the call created are removed, so nothing is left behind. The source is never
  written.
- **Manifest.** `name` is lowercased and non-name characters become `-`; it is
  refused if no valid name remains. The portable string fields and `keywords`
  are copied; a string `author` becomes `{"name": ...}`. Every other key is
  listed in `not_converted`; the generated `plugin.json` carries no record of
  the conversion.
- **Skills.** From the manifest's `skills` path or list, else `skills/`,
  `.claude/skills/` and `.agents/skills/`. A declared folder holding `SKILL.md`
  is one skill; otherwise its immediate child skill folders are scanned. A
  declared path yielding no skill is noted. Each folder holding `SKILL.md`
  becomes `skills/<frontmatter name>/`, or keeps its folder name when the
  frontmatter has no valid name or `SKILL.md` exceeds the loader's 1 MiB limit
  (then it is not read). A declared `.` or `./` with a root `SKILL.md` makes the
  package root one skill: everything the package copy holds except its
  top-level `skills/` (whose other entries are kept as below); without a valid
  frontmatter name it takes the plugin name, or is noted and not placed when
  that is not a valid skill name either. Every placed skill is copied into a
  fresh folder in a second hidden temporary folder next to `dest`, outside the
  staging folder, which then replaces `skills/`; no copy's source contains its
  destination, and renames that chain or differ only in case lose nothing.
  These copies have the same 20,000-file and 200 MiB bounds as the package
  copy, counted separately. Names are compared case-insensitively; the first
  claim wins and each dropped skill is named in `notes` and left out of
  `skills/`. Other entries of the old `skills/` are kept unless a placed skill
  takes their name, which is also noted.
- **MCP.** From the manifest's inline `mcpServers` or the file it names, else
  `.mcp.json`, else `mcp.json`. A named or default file may hold either an
  `mcpServers` wrapper or a direct map whose values are server objects; any other
  shape is noted and contributes no servers. Per server: a missing `type` becomes
  `stdio` (with `command`) or `streamable-http` (with `url`); `http` becomes
  `streamable-http`; `${CLAUDE_PLUGIN_ROOT}` becomes `${PLUGIN_ROOT}`; a bundled
  relative command or `cwd` gains `./`; fields outside the closed set are dropped.
  A server that needs any other `${VAR}` (found by a scan of every key and string
  value for `${` up to the next `}`), holds a `${` with no closing `}`, has an
  absolute command or `cwd`, a
  path command that resolves outside the package or is not a file there, a
  `cwd` outside the package, an unknown transport, or that the loader would
  reject is listed in `skipped_servers` and not written. The output `mcp.json`
  is exactly the converted servers, or absent when none convert; a client file
  already at `mcp.json` is never kept, and its removal or replacement is noted.

## 5. Side effects

Conversion writes only under its staging folder and `dest`. Discovery reads files only: it does not create `data/<name>/`, write any
index, start a process or add a skill root. Each read is size-bounded
(`plugin.json` and `mcp.json` 256 KiB, `SKILL.md` 1 MiB, `installed.json` 1 MiB).
