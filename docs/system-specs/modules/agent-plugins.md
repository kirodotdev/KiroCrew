# Agent Plugins

Owner: `kiro_crew.agent_plugins`. Tests: `test/test_agent_plugins_discovery.py`,
`test/test_agent_plugins_mcp.py`, `test/test_agent_plugins_conformance.py`
(which pins the loader's verdict on the official conformance suite's own fixtures,
vendored in `test/fixtures/agent_plugins_conformance/`),
`test/test_agent_plugins_install.py`, `test/test_agent_plugins_consent.py`,
`test/test_agent_plugins_routes.py`, `test/test_agent_plugins_launch.py`,
`test/test_agent_plugins_mcp_source.py`, `test/test_agent_plugins_include_powers.py`
and `test/test_agent_plugins_skills_catalog.py`.
Design:
[rfc-agent-plugins-standard.md](../../request-for-change/rfc-agent-plugins-standard.md).

Kiro Crew's client for the [Agent Plugins](https://agent-plugins.org/) 1.0.0
standard. This module discovers, validates, converts and installs plugins, keeps
the owner's on/off record, and starts the stdio servers of plugins the owner
turned on. A plugin's MCP servers reach sessions through the plugin MCP source
(§9), and its skills through the skill catalog (§12).

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
| `off` | A valid plugin the owner has not turned on. Its skills, servers and per-component issues are filled in |
| `on` | A valid plugin the owner turned on (§7) |

`on` is computed from the owner's record (`consent.enabled()`), read once per
call unless the caller passes it. Consent is to a plugin by name: editing or
updating the files of an `on` plugin keeps it `on`, and a plugin that becomes
`invalid` is `invalid` whatever the record says.

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
Cursor package into a standard plugin, following the spec's migration guide. The
install flow (§5) is its caller. Tests:
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
  A server that needs any other `${VAR}`, has an absolute command or `cwd`, a
  path command that resolves outside the package or is not a file there, a
  `cwd` outside the package, an unknown transport, or that the loader would
  reject is listed in `skipped_servers` and not written. The output `mcp.json`
  is exactly the converted servers, or absent when none convert; a client file
  already at `mcp.json` is never kept, and its removal or replacement is noted.

## 5. Installing

`agent_plugins.install` puts a plugin into the powers home the way Kiro IDE and
kiro-cli do, so it also appears in Kiro. Tests: `test/test_agent_plugins_install.py`.

- **`install_from_folder(src, home=, replace=)`.** A folder that
  `find_client_manifest` calls standard (a root `plugin.json` with an
  `agent-plugins.org` `$schema`), or that has no manifest at all, is copied with
  the converter's package copy: links and junctions are neither followed nor
  copied, `.git` and `node_modules` are skipped, and the 20,000-file and 200 MiB
  bounds apply. Any other folder goes through `convert_plugin`. The copy is built
  in a hidden `installed/.installing-*` folder, then validated with the loader
  against that staging folder as its home; `invalid` and `not-a-plugin` are
  refused. A source that overlaps `installed/` is refused.
- **Name and replace.** The install name is the manifest `name`. A name already
  listed in `installed.json`, or already present as a folder, is refused unless
  `replace` is set; then the old folder is moved aside and the new one moved in
  with two `os.replace` calls, and the old one is restored if the second move or
  the index write fails. `data/<name>/` is never touched. Consent is by name
  (§7), so replacing a plugin that is `on` keeps it `on`.
- **Index.** `installed.json` is read (a missing file is an empty index; an
  unreadable, oversized or non-JSON one is refused, never overwritten), and
  `{"name": <name>}` is appended unless the name is already listed. Every other
  entry, every unknown field and the key order are kept. The file is rewritten
  with `atomic_write` while `.installed.json.lock` in the powers home is held.
- **Result.** `InstallResult` carries the name, the loader record for
  `installed/<name>/` (always `off`: installing never turns a plugin on), the
  conversion report (`None` for a standard source), copy notes and `replaced`.
  A refused install leaves `installed/` and `installed.json` as they were, and
  removes any folder the call created.
- **`install_from_git(url, ref=, subdir=, home=, replace=)`.** Only `https://`
  URLs with a host and no user information, fragment or whitespace are accepted;
  `file://`, `ssh://`, `git@host:path`, `<transport>::` addresses and local paths
  are refused before git runs. `ref` must be a plain branch or tag name; `subdir`
  must be relative, free of `..`, reached through no link and inside the clone.
  The clone is `git -c protocol.allow=never -c protocol.https.allow=always -c
  core.symlinks=false -c core.hooksPath=<devnull> -c core.fsmonitor=false clone
  --depth 1 --no-recurse-submodules --template= [--branch <ref>] -- <url> <dest>`
  into a fresh system temp folder, with `GIT_TERMINAL_PROMPT=0`, the
  repository-redirecting `GIT_*` variables removed, stdin closed, UTF-8 output
  decoding and a 300-second timeout. The temp folder is removed afterwards.
- **`uninstall(name, home=)`.** Removes every `installed.json` entry for the
  name, then moves `installed/<name>/` aside and deletes it (a link is unlinked,
  never followed). `data/<name>/` is kept. A name neither listed nor present is
  refused.
- **Routes.** `POST /api/agent-plugins/install` (`{"path"}` or `{"url", "ref"?,
  "subdir"?}`, optional `"replace"`) and `POST /api/agent-plugins/uninstall`
  (`{"name"}`), in `dashboard/handlers/agent_plugins.py`. Only the dashboard
  owner may call them (`is_owner_dashboard_request`, the AWS consent rule); an
  app token or an allow-listed messaging user gets 403. Work runs off the event
  loop. Refusals carry the `InstallError.code` (`agent_plugin_*`); a duplicate is
  409, an unknown name 404, a failed clone 502, anything else 400. There is no
  MCP tool or CLI verb for either.

## 6. Side effects

Conversion writes only under its staging folder and `dest`. Install writes only
`installed/`, `installed.json` and `.installed.json.lock`, and runs only `git clone`. Discovery reads files only: it does not create `data/<name>/`, write any
index or the owner's record, start a process or add a skill root. Each read is size-bounded
(`plugin.json` and `mcp.json` 256 KiB, `SKILL.md` 1 MiB, `installed.json` 1 MiB).

## 7. The owner's on/off record (`agent_plugins.consent`)

`agent-plugins.json` in the data home (`config_dir()`):

```json
{"version": 1, "enabled": {"<name>": {}}}
```

- **By name.** Each key of `enabled` is a plugin the owner turned on; its row is
  an empty object. `enabled()` returns the set of those names. A row's contents
  are ignored, so a row an earlier build wrote with extra fields still reads as
  on, and the next write stores it as `{}`.
- **Reads fail closed.** A missing, empty, oversized (past 1 MiB), unreadable or
  malformed record, a wrong `version`, or a row that is not an object, means
  that plugin (or every plugin) is not on.
- **Writes.** `set_enabled(name, on)` is called only by the dashboard
  handler (§8). It serialises on an in-process lock (every writer runs in the
  gateway), copies an unparseable record aside as `agent-plugins.json.corrupt-<UTC>`,
  and publishes through `atomic_write` with owner-only permissions. Turning a
  plugin on adds its row; turning one off removes it.
- **Sandbox placement.** The leaf is in `sandbox_leaves._CREW_READONLY_LEAVES`
  (sealed read-only for sandboxed code), `sandbox_precreate._CREW_PRECREATE_READONLY_FILE_LEAVES`
  (pre-created as `{}`, which reads as nothing on), and `security.paths_lists._CREW_SECRET_LEAVES`
  (fenced from the agent's file tools), the placement `aws_service_consent.json`
  has. Unlike that record it is in `_CREW_CHILD_READABLE_LEAVES`, not the
  enforced-harness withheld list: the launcher (§10) runs as a harness child and
  must read it, it holds only plugin names, and the read-only seal answers
  the write that would turn a plugin on.

There is no MCP tool and no CLI verb that writes the record.

## 8. Dashboard routes (`dashboard/handlers/agent_plugins_consent.py`)

| Route | Body | Result |
|---|---|---|
| `GET /api/agent-plugins` | | `{"plugins": [{name, state, reason, version, description, servers, skills, issues}]}` |
| `POST /api/agent-plugins` | `{"name": str, "enabled": bool}` | `{"ok": true, "name", "state"}` |

Both refuse anyone but the dashboard owner (`is_owner_dashboard_request`) with
403 `dashboard_owner_required`, before reading the body: an app token, an allowed
messaging user and an unauthenticated caller are all refused. Only `name` and
`enabled` are read from the body. An unknown name is 404 `agent_plugin_not_found`;
an `invalid` or `not-a-plugin` plugin is 409 `agent_plugin_not_enableable`; a
malformed body is 400. Each switch is recorded in
the Security Event Log (`agent_plugins.enabled` / `agent_plugins.disabled`). The
routes are registered in `dashboard/routes/system.py` beside the AWS consent
routes. A change reaches sessions at their next spec rebuild.

## 9. MCP source (`agent_materialization/mcp_sources.py`)

`merge_mcp_sources()` projects the servers of every `on` plugin second in the
chain: after the app servers, before the Kiro global `mcp.json`, the provider
globals and the dashboard store. So an app's same-named key wins, and a
leftover in a shared file cannot stand in for a plugin entry.

- Each valid server is keyed `plugin-<name>:<server>`; characters of the server
  name outside `[A-Za-z0-9_.-]` become `-`.
- A stdio server becomes the launcher: `isolated_python_argv("-P", "-m",
  "kiro_crew.agent_plugins.launch", <name>, <server>)`. A remote server becomes
  `{"url"}`, plus `"type": "sse"` for SSE and its `headers`.
- Each entry goes through `auto_approve._ceiling_filtered_spec` and is exposed
  in `tools` as `@<key>`.
- Entries are re-derived every rebuild and assigned, not `setdefault`ed. A
  `plugin-` key a previous rebuild wrote that this one does not derive (plugin
  turned off, now `invalid`, or uninstalled) is removed with its `@ref`.
- The source never raises: a discovery failure contributes nothing, and a
  failure on one plugin skips only that plugin.

A host with no `on` plugin gets no `plugin-` key.

## 10. The stdio launcher (`agent_plugins.launch`)

`python -P -m kiro_crew.agent_plugins.launch <plugin> <server>`. Every harness,
KAS included (which drops `cwd` from MCP entries), gets the same launch rules
through it. In order it:

1. re-reads the owner's record, re-inspects that one plugin and refuses unless
   it is `on` (valid and in the record) and declares a valid stdio server by
   that name;
2. creates `<powers home>/data/<plugin>/`;
3. expands `${PLUGIN_ROOT}` and `${PLUGIN_DATA}` once, in a single left-to-right
   pass, in `args`, `env` values and `cwd` (§9.2); other `${...}` stays literal;
4. copies its own environment, overlays the declared `env`, then sets
   `PLUGIN_ROOT` and `PLUGIN_DATA` last (§9.1);
5. resolves `command` as one token: a bare name through the resulting `PATH`, a
   `./` path against the root, which must stay a regular file inside it;
6. changes to `cwd` (the root when omitted; a `./` value is relative to the
   root) and calls `execve`. On Windows the server runs as a child on the
   launcher's stdio and its exit status is passed on.

A refusal prints one stderr line, `kirocrew: plugin MCP server <plugin>:<server>
was not started: <reason>.`, and exits 78; the server is never started.

## 11. `includePowers`

Every spec Crew writes pins `includePowers: false` wherever it pins
`includeMcpJson: false` (`agent._refresh_dynamic_fields`,
`auto_approve.govern_spec`, `service_agents`, `spec_rebuild._write_heartbeat_spec`,
`connections/mint._mint_spec_body`, `side_readonly_spec.derive_readonly_spec`),
and `apps/bridges_agents._register_agents` sets it on every app agent. KAS's own
plugin loader therefore never runs beside Crew's, and no server is registered
twice. `acp/kas_agents.to_client_custom_agent` forwards the bool, and
`client_mcp_array.spec_is_toolless` still treats a `false` value as toolless.

## 12. Skills (`agent_plugins.skill_roots`)

`on_plugin_skill_roots()` gives the skill catalog one `PluginSkillRoot` per `on`
plugin: the plugin's resolved `skills/` directory and the skills discovery
validated in it (§3), each keyed `plugin-<name>/<skill>`, so two plugins with a
skill of the same name never collide.

- **Trust.** `skills_discovery._trusted_skill_roots()` adds each root's
  directory, one per `on` plugin, never `installed/` or a plugin root as a
  whole. An `off`, `invalid` or `not-a-plugin` plugin adds none, so a
  link placed in the skills dir that points into its skills is refused.
- **Catalog.** `_iter_uncached` lists every root's skills after the skills dir
  and before `extra_paths`, vetted through `validate_file_path`, and `load_skill`
  serves them by key in the same order. `skill_search`, the startup index and the
  native skill view of every harness read the same catalog, so there is no
  per-harness branch. A skill folder that links elsewhere inside the plugin
  resolves outside the trusted directory and is left out.
- **Freshness.** The answer is re-derived from discovery. It is kept while the
  owner's record and `installed.json` are unchanged, for at most 60 seconds, the
  catalog's own revalidation period. Turning a plugin off or uninstalling it
  rewrites one of those files, so the next catalog refresh drops its skills. A
  skill added to or removed from an `on` plugin is seen within the 60 seconds,
  and an edit to a skill's text keeps it served.
- **Failures.** It never raises: a discovery failure contributes no root, and a
  failure on one plugin skips only that plugin.
