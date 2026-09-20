# Plugin import

Owner: `kiro_crew.apps.plugin_import`. CLI: `kirocrew app import`. Tests:
`test/test_plugin_import.py`.

Converts a manifest-declared plugin package into an installable Kiro Crew app.
The input format is the one Codex reads and the one the published plugin
directories ship; grok-build's plugin directory bundles the same resource kinds.
Which foreign kind reaches which Kiro Crew extension point, and why the rest do
not, is [harness-plugin-mapping.md](harness-plugin-mapping.md) -- this file
specifies the converter, not the mapping.

## 1. What it is for

Two claims needed proving before Kiro Crew accepts foreign plugin contributions:
that the extension points we already have can receive them, and that receiving
them does not require running foreign code. A converter proves both. It reads
JSON and copies files; the emitted app is an ordinary Kiro Crew app with no trace
of the source runtime and nothing to supervise.

It is deliberately not an adapter. A package's in-process code -- tools, hook
callbacks, services -- is not converted and cannot be: that half needs a process
hosting the foreign runtime, which is the [contribution
protocol](contribution-protocol.md)'s section 8 and a separate piece of work.

## 2. Input

A directory whose manifest is found in this order:

1. `plugin.json` at the package root, accepted only when its top-level `$schema`
   is a string beginning `https://agent-plugins.org/schemas/`. A `plugin.json`
   without that is some other file sharing the name, and discovery falls through.
2. The first `.*-plugin/plugin.json` in sorted order -- the vendor-prefixed
   locations (`.codex-plugin`, `.claude-plugin`, `.cursor-plugin` and any other
   of that shape). Matching by shape rather than by a vendor allowlist means a
   fourth vendor directory needs no code change; sorting makes a package that
   carries several resolve identically on every machine.

Neither candidate is accepted through a symlink.

Manifest fields read, all camelCase, every path written `./`-relative:

| Field | Shape | Becomes |
|---|---|---|
| `name` | string | `name`, folded to kebab-case |
| `version` | string | `version` when semver, else `0.0.0` and a warning |
| `description` | string | `description` |
| `author` | object with `name`, or string | `author` |
| `license` | string | `license` |
| `keywords` | list of strings | `tags` |
| `skills` | path, or list of paths | `skills`, resources copied |
| `mcpServers` | path to a config file, or inline object | `mcpServers` |
| `apps` | path | reported, not converted |
| `hooks` | path, list of paths, inline object, or list of objects | reported, not converted |
| `interface.displayName` | string | `displayName` |
| `interface.shortDescription`, `.longDescription` | string | `description` fallback |
| `interface.developerName` | string | `author` fallback |
| `interface` icons, colors, prompts, links; `homepage`; `repository` | -- | carried as provenance |

`displayName` and `description` are required by an app manifest and optional in
the source, so both are synthesized when absent, with a warning naming what was
synthesized. A key the converter does not read is named in a warning rather than
ignored silently.

## 3. Output

```
<out>/app.json
<out>/skills/<skill-name>/...        one directory per discovered skill
```

`app.json` carries the conversion in an `importedPlugin` block. `extra` on an app
manifest is preserved through `from_dict`/`to_dict`, so the block survives
install and is readable off the installed app:

```json
"importedPlugin": {
  "sourceFormat": "vendor-directory",
  "sourceManifest": ".codex-plugin/plugin.json",
  "appName": "acme-tracker",
  "mapped":   [{"kind": "skills", "target": "app.json skills", "detail": "3 skill(s) copied"}],
  "unmapped": [{"kind": "apps", "bucket": "d", "reason": "...", "detail": ""}],
  "warnings": [],
  "carried":  {"brandColor": "#FF584A", "homepage": "https://acme.test"}
}
```

`unmapped[].bucket` is the mapping doc's bucket letter, so a reader of an
installed app can go from the app to the row that explains what was left behind.

A skill is a directory containing `SKILL.md`, discovered breadth-first under each
declared skills root (default `skills/` when the manifest declares none). The
search stops descending at the directory that holds the entry file, so a skill's
own subdirectories travel with it.

The emitted manifest is validated with `AppManifest.validate` before it is
written. A conversion that would emit an invalid manifest raises
`emitted_manifest_invalid` and writes nothing.

### 3.1 A server whose program lives in the package is refused

The source format resolves a server's `command`, `args` and `cwd` against the
package root. Conversion does not preserve that root, and the program a server
points at is not a DECLARED resource, so it is not copied either. Emitting such a
server would register one that cannot start -- and a stdio server that fails to
launch takes its whole tool set with it, silently, on an app that installed clean.

So a server config carrying a package-relative path is refused as a unit and
reported, naming the fields:

```
mcpServers[acme] [d] the server resolves its program against the source package
  root, which conversion does not preserve, and that program is not a declared
  resource so it is not copied -- package-relative: args[0], cwd
```

Detection is narrow on purpose -- `.`, `..`, an explicit `./` or `../` prefix, and
any non-absolute `cwd` -- so a bare command name (`npx`), a flag (`-y`) and a
package specifier (`some-mcp@latest`) are never mistaken for a path. One refused
server does not take its siblings with it: a map with a bare-command server and a
package-relative one emits the first and reports the second.

## 4. The two safety properties

**No foreign code runs.** Conversion opens JSON files and copies bytes. Nothing
in the package is imported, evaluated, or spawned -- at conversion time or after.
A hook's `command` string is data. Pinned by
`TestUnmappedKinds::test_conversion_runs_no_command_from_the_package`, which
gives the package a hook that would create a sentinel file and asserts the file
does not exist.

**The package root is the authority boundary.** Every declared path must start
with `./`, must not contain a `..` segment, must not be rooted (leading `/`, a
leading `\`, or a drive letter), and must still resolve under the package root
after symlinks are resolved. A path that escapes raises `resource_outside_root`
and fails the whole conversion; nothing partial is left behind. The same rule
governs the tree walk: a symlink found *inside* a copied resource is skipped and
reported, never followed, because following one would put a file from outside the
package inside an installed app. Pinned by `TestDeclaredPathContainment` and
`TestSkills::test_a_symlink_inside_a_skill_is_skipped_not_followed`.

Requiring the `./` prefix rather than normalizing a bare `skills` is deliberate:
a reader that only strips a leading dot-slash also accepts `/etc`.

## 5. Bounds

Third-party input, so every loop over it has a ceiling and hitting one is a
reported warning, never a silent trim: at most 200 skills per package, 8 levels
of skill-tree depth, and 32 MiB per copied file. A duplicate skill directory name
is skipped with a warning rather than overwriting its predecessor.

Converting twice into the same output directory is refused with
`output_not_empty`: overwriting would let a second run merge two packages into
one app.

## 6. Error codes

Every refusal carries a stable `code` on `PluginImportError`:
`source_not_a_directory`, `manifest_not_found`, `manifest_unreadable`,
`manifest_not_json`, `manifest_not_object`, `invalid_declared_path`,
`resource_outside_root`, `invalid_app_name`, `reserved_app_name`,
`output_not_empty`, `emitted_manifest_invalid`.

## 7. CLI

```
kirocrew app import <package-dir> [--out DIR] [--name NAME] [--install]
```

Prints the mapped and not-mapped halves plus warnings, then the path written.
`--out` defaults to `./<app-name>-app`. `--name` overrides the derived app name,
which is how a package whose name folds onto a reserved app name is imported.
`--install` runs the ordinary local install afterwards; without it the command
prints the `kirocrew app install` line to run. Install leaves an app disabled, as
every local install does -- `kirocrew app enable <name>` is the separate step.

## 8. What it was measured against

The plugin directory published at `github.com/openai/plugins`, tree `d416fd5`,
converted in one pass:

| | |
|---|---|
| packages | 62 |
| converted | 62 |
| refused | 0 |
| emitted an invalid manifest | 0 |
| skills copied | 501 |
| MCP servers mapped | 4 |
| MCP servers refused as package-relative | 4 |
| connector declarations reported unmapped | 36 |
| presentation and link blocks carried | 62 |
| hook declarations reported | 1 |
| warnings | 0 |

Zero warnings across 62 real manifests is the number that matters: every field
every published package declares is either mapped to an extension point or
reported as unmapped with a reason. Nothing in that corpus was dropped silently.

Exactly half the MCP servers in the corpus are package-relative and therefore
refused, which is why §3.1 exists: the shape is not an edge case.

Three of those packages were then converted, installed and enabled in an isolated
pod, chosen to cover the three outcomes: one whose bare-command server registered
into the agent config as `<app>:<server>` alongside 9 skills, one whose 14 skills
mapped while its package-relative server was refused, and one that declares hooks.
