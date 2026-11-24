# Artifacts and apps commands

Moved verbatim from the kirocrew-commands `SKILL.md` core.

## Artifacts

LLM-generated UI components (widgets, HTML, markdown, SVG, JSON, text).

| Command | Description |
|---------|-------------|
| `kirocrew artifact list` | List all artifacts |
| `kirocrew artifact list --tag ops --kind widget` | Filter by tag and kind |
| `kirocrew artifact list -q "CR"` | Substring filter on name |
| `kirocrew artifact show SLUG` | Print artifact content |
| `kirocrew artifact show SLUG --version 2` | Show specific version |
| `kirocrew artifact show SLUG --meta` | Show metadata as JSON |
| `kirocrew artifact show SLUG --docx out.docx` | Write a markdown or text artifact as a new Word file, one paragraph per line, headings styled |
| `kirocrew artifact save --name "My Widget" --content-file widget.html` | Save new artifact |
| `kirocrew artifact save --name "X" --content "..." --tags ops,cr` | Save with inline content |
| `kirocrew artifact update SLUG --content-file widget.html` | Update artifact content |
| `kirocrew artifact update SLUG --name "New Name" --tags ops` | Rename/retag |
| `kirocrew artifact versions SLUG` | List version numbers |
| `kirocrew artifact delete SLUG` | Delete artifact and all versions |

## Apps

| Command | Description |
|---------|-------------|
| `kirocrew app list` | List installed apps |
| `kirocrew app install /path/to/app-dir` | Install app from local directory (needs app.json) |
| `kirocrew app install GIT_URL [--ref REF] [--subdirectory DIR]` | Clone a git repo (`https://`, `ssh://` or `git@host:org/repo.git`) with your own git setup, then install the folder holding `app.json`; `--ref` picks a branch or tag. A URL with a password or token, or a plaintext `http://`/`git://` URL, is refused |
| `kirocrew app enable NAME` | Enable an installed app (an app that asks to control your chats is allowed in the dashboard only) |
| `kirocrew app disable NAME` | Disable an installed app |
| `kirocrew app update NAME [--source DIR]` | Update an installed app in place from its source directory (the one it was installed or last updated from, unless `--source` names another, which then becomes the recorded one), keeping its data and enabled state. Needs a running gateway; with none reachable it changes nothing |
| `kirocrew app uninstall NAME` | Uninstall an app and preserve its data directory |
| `kirocrew app uninstall NAME --purge-data` | Uninstall and explicitly delete the app data directory |
| `kirocrew app info NAME` | Show app details |
| `kirocrew app config NAME` | Read an installed app's config, the same as `get` (needs a running gateway; not on native Windows) |
| `kirocrew app config NAME get` | Print the app's config |
| `kirocrew app config NAME set KEY VALUE` | Set one top-level key, taken literally (`a.b` is one key), with VALUE typed as `kirocrew config set` types it (JSON for an object). It runs none of the app's own checks and works only for an app whose config the host stores |
| `kirocrew app init NAME` | Scaffold a new app (kebab-case name) |
| `kirocrew app init NAME --backend --ui --cron` | Scaffold with backend, UI, and sample cron |
| `kirocrew app import SOURCE` | Convert a manifest-declared plugin package into an app directory (`--out DIR`, `--name NAME` to override the derived name, `--install` to install it after writing) |
| `kirocrew app dev NAME` | Toggle an app into dev mode (no-store UI serving + live reload on file change) |
| `kirocrew app dev NAME --off` | Leave dev mode |
| `kirocrew app mcp NAME` | Run an app's MCP server on stdio (spawned by kiro-cli, not for humans) |
