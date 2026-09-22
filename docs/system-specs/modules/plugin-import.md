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
that the extension points we already have can receive them, and that conversion
does not require running foreign code. A converter proves both. It reads JSON
and copies files; the converter itself has no foreign runtime to supervise. The
emitted app is an ordinary Kiro Crew app, so a mapped MCP server can be launched
after installation under the normal app lifecycle.

It is deliberately not an adapter. A package's in-process code -- tools, hook
callbacks, services -- is not converted and cannot be: that half needs a process
hosting the foreign runtime, which is a separate piece of work.

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

**The source root's ANCESTORS are checked before the root itself.** A leaf-only link
test still resolves through a linked PARENT, and the `is_dir()` that follows is the
probe that does it: on Windows an ancestor junction whose target is a UNC share turns
that call on a local-looking path into an outbound SMB connection authenticating as
this process, to a host the PACKAGE chose. A lexical UNC screen cannot catch it,
because the path being probed is not itself UNC-shaped -- only the link's target is.
`first_linked_ancestor` walks root-first and stops at the first hit, so each `lstat`
runs only after every ancestor above it is known not to be a link and the walk itself
never traverses one; it is paired with the leaf test, which is the same pairing the
essential-context reader already uses. The offending ancestor is left out of the
message: which ancestor is a link is filesystem layout the caller supplied.

**An entry naming no transport is dropped before any of that.** A server is declared
with a program to run (`command`) or an endpoint to connect to (`url`); an entry
carrying neither names no server at all. Nothing else in the conversion catches it,
because an empty or transportless object is a valid object, holds no
package-relative field to refuse, and has nothing over-limit for the bounding pass
to trim -- so it was emitted into app.json as a server with no way to reach it,
which no reader can launch and no message mentioned. This is asked separately from
whether a transport is USABLE, which is the package-relative question above, and it
is declined the same way every other invalid entry is: the entry is dropped and
named, and the import goes on.

Detection asks one question: does this value resolve against a DIRECTORY rather
than against `PATH`? Three shapes answer yes. A value carrying a separator, in
either spelling, because both `bin/server` and `.\bin\server` resolve against the
session's working directory and not against the package. `.` and `..`, which are
directories with no separator at all. And a separator-less token carrying a program
suffix (`server.js`, `app.py`, `server.exe`), because that names a FILE: on Windows
a bare program name is not a `PATH` lookup at all, since `CreateProcess` searches
the calling process's directory and the current directory first, and on every
platform such a program cannot start after conversion anyway. Any non-absolute
`cwd` answers yes as well.

Three shapes carry a slash or a dot without naming a path and are excluded by name,
so the rule does not start refusing servers that convert correctly: a flag (`-y`),
an npm scope specifier (`@scope/pkg`) and a URL (`git+https://...`). The flag
exclusion covers the FLAG, never a path it carries: the value after the first `=` is
re-tested by the same rule, so `--config=./local/thing` and `--plugin=bin/server`
answer yes while `--pkg=@scope/x`, `--src=git+https://...` and a bare `--quiet`
answer no. Excluding every token that began with a dash was how the shape this
detector exists to catch got back in through option syntax: such a value resolves
against the session's working directory exactly as the bare spelling does. A bare
command
(`npx`), a package specifier (`some-mcp@latest`) and a version-suffixed interpreter
(`python3.11`) are never mistaken for a path -- the test is the program suffix, not
the presence of a dot. One refused
server does not take its siblings with it: a map with a bare-command server and a
package-relative one emits the first and reports the second.

## 4. The two safety properties

**No foreign code runs during conversion.** Conversion opens JSON files and
copies bytes. Nothing in the package is imported, evaluated, or spawned while
the converter runs. After installation, mapped MCP server commands follow the
ordinary app lifecycle; package-relative programs are refused by §3.1. A hook's
`command` string remains data. Pinned by
`TestUnmappedKinds::test_conversion_runs_no_command_from_the_package`, which
gives the package a hook that would create a sentinel file and asserts the file
does not exist during conversion.

**A linked source root is refused before anything probes it.** A probe resolves the
path, so on Windows a junction whose target is a UNC share makes the OS authenticate
to a host the package chose, and a refusal that runs afterwards is too late. Every
DECLARED path is covered by the segment walk described below; the ROOT is what those
are resolved against, so it carries its own check, answered as
`source_not_a_directory`.

**The package root is the authority boundary.** Every declared path must start
with `./`, must not contain a `..` segment, must not be rooted (leading `/`, a
leading `\`, or a drive letter), and must still resolve under the package root
after symlinks are resolved. A path that escapes raises `resource_outside_root`
and fails the whole conversion; nothing partial is left behind. The same rule
governs the tree walk: a link found *inside* a copied resource is skipped and
reported, never followed, because following one would put a file from outside the
package inside an installed app. "Link" is
`platform_compat.is_link_or_junction`, not `Path.is_symlink`: a Windows directory
junction answers False to `is_symlink`, so that check alone reads a junction as an
ordinary directory and copies its target -- a junction to `.ssh` would put private
keys in the emitted app. The link check on a DECLARED path runs before the path is resolved, not after: `.resolve()` traverses a junction, and one aimed at a UNC share makes Windows authenticate to that host mid-resolve, so a refusal that waits for the containment check has already leaked the operator's credentials. Pinned by `TestDeclaredPathContainment`,
`TestSkills::test_a_symlink_inside_a_skill_is_skipped_not_followed` and
`TestLinksAndJunctions`.

Requiring the `./` prefix rather than normalizing a bare `skills` is deliberate:
a reader that only strips a leading dot-slash also accepts `/etc`.

## 5. Bounds

Third-party input, so every loop over it has a ceiling and hitting one is a
reported warning, never a silent trim: at most 200 skills per package, 8 levels
of skill-tree depth, 32 MiB per copied file, and per skill tree 2000 files and
128 MiB in total. The last two are a separate question from the per-file bound and
from depth: depth limits how DEEP a walk goes and never how wide, so a tree of many
small files, or of files each just under the per-file cap, was unbounded. The
allowance is shared across the whole recursion, because a per-directory cap is not
a cap on a tree, and it is spent only by a copy that landed.

The same bound applies to the schema PROBE, which is reached by more inputs than
the manifest read is: it runs on a root `plugin.json` for every candidate,
including a directory that turns out not to be a plugin.

A failed publish RESTORES an output directory it removed. The move needs the
destination absent, so an empty directory the caller made is removed first -- and
if the move then fails, that removal would be the only lasting effect of a command
that reported failure.

Every field the report renders comes from the foreign manifest and is printed on
its own prefixed line, so each goes through the repo's shared one-line terminal
sanitizer. A field carrying an escape could repaint the screen; one carrying a
newline could open a line that reads as the tool's own output. The command's own
refusals go through it for the same reason: each one embeds a path taken from the
package, and it is printed after a `❌`. The derived output directory does not need
it, because `normalize_app_name` folds every non-alphanumeric character to a hyphen
and then requires a full kebab-case match, so no control character survives.

**A value with no JSON spelling is dropped, not carried.** `json.loads` accepts the
bare words `NaN`, `Infinity` and `-Infinity`, and `json.dumps` re-emits them, but
RFC 8259 defines none of them -- so one non-finite number in a malformed manifest
would produce an app.json that a strict reader refuses whole. Unlike an over-long
string there is no shorter form to keep, so the bounding pass refuses the field the
way it refuses an over-deep one: the field is dropped and reported, and the entry
around it survives.

**A directory listing is bounded at the ITERATOR, not at the result.** `sorted()`
has to exhaust its iterator before it can order anything, so a cap applied to what
comes back runs after the whole directory is already in memory -- a package shipping
a directory of a million names is materialised in full by the sort, whatever the
later limit says. Every listing here goes through one helper that takes at most
`MAX_DIR_ENTRIES` and then looks ahead exactly one entry to say whether that was all
of them. The vendor-manifest glob is bounded the same way, because a glob walks the
directory too.

A manifest is refused UNREAD past 4 MiB. Every other bound here applies to a value
already parsed, which is the right place for a bound on what is retained, but a
manifest is read whole in one call -- so this is the only bound that can run before
the memory is spent, and it is asked of the filesystem rather than measured from
the string.

The 200-skill ceiling is applied WHILE the tree is walked, not only where the
skills are emitted. A package can be wide as well as deep, so a walk that
discovered the whole population before anyone counted it had already spent the
memory the ceiling exists to bound, and its frontier was a list popped at index
zero, which made a breadth-first walk quadratic in its own width.

**The search FRONTIER carries its own ceiling, because the skill ceiling bounds the
answer rather than the search.** The skill counter advances only where a directory
holds an entry file, so a tree carrying no marker anywhere never advances it while
every subdirectory still joins the frontier -- the same retained-versus-inspected
distinction the server ceiling is written to. Depth does not bound it either: each
level may contribute `MAX_DIR_ENTRIES` directories, so the population multiplies per
level. The frontier is bounded directly at `MAX_SKILL_TREE_DIRS`, and the depth limit
is applied where a directory is ENQUEUED rather than where it is popped, so an
over-deep subtree is never held in memory to be discarded afterwards.

**A manifest container is bounded by entries INSPECTED, not by entries kept.** Every
branch that drops an entry -- a non-string key, an over-long key, a non-finite
number, an over-deep value -- appends a warning and keeps nothing, so a ceiling on
the retained container never fires on precisely the inputs that grow the report. A
container of nothing but dropped entries left the retained count at zero forever and
grew the warning list once per input entry.

**A hooks declaration is bounded at `MAX_HOOK_DOCUMENTS` entries inspected.** Nothing
rejects a repeat, so a manifest may declare one file any number of times and each
declaration is opened, parsed and retained again: the per-file byte budget bounds one
read, never the total. A ceiling on the retained document list would not serve
either, since a missing path appends a warning and retains no document.

The 100-server ceiling counts entries INSPECTED, not entries emitted. Every server
this converter refuses still retains something for the operator to read -- a warning,
or an unmapped record naming the server and the reason -- so a ceiling on the emitted
dict bounds none of it: a manifest of nothing but refused servers leaves that dict
empty forever and grows the report with the input. A duplicate skill directory name
is skipped with a warning rather than overwriting its predecessor.

**The skip-description allowance is spent ON APPEND, and it is shared across the
whole tree.** `MAX_SKIP_DESCRIPTIONS` bounds what the copy walk retains to describe
the entries it declined. Trimming each directory's finished list to that number
bounds neither the peak nor the total: every description is a formatted string
carrying a full path, so the memory the cap exists to bound is already spent by the
time a trim can see it; and the parent extends each child's already-trimmed list, so
a walk over N directories holds N times the cap. The allowance therefore lives in the
same budget the walk shares for files and bytes, and is decremented per description.
Overflow past it is COUNTED rather than dropped, and the count is reported once, by
the call that created the budget, so a recursion cannot emit one summary per
directory each naming the same running total.

Converting twice into the same output directory is refused with
`output_not_empty`: overwriting would let a second run merge two packages into
one app.

## 6. Error codes

Every refusal carries a stable `code` on `PluginImportError`:
`source_not_a_directory`, `manifest_not_found`, `manifest_unreadable`,
`manifest_not_json`, `manifest_not_object`, `invalid_manifest_field`,
`invalid_declared_path`, `resource_outside_root`, `invalid_app_name`,
`reserved_app_name`, `output_not_empty`, `output_not_a_directory`,
`output_not_readable`, `output_within_source`, `output_is_a_link`,
`staging_unwritable`, `output_publish_failed`, `emitted_manifest_invalid`.

`test_the_spec_lists_every_code_the_module_raises` compares that list against the
codes in the module, so the two cannot drift apart silently.

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
