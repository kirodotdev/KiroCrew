# Pods and browsing commands

Moved verbatim from the kirocrew-commands `SKILL.md` core.

## Pods (Isolated Worktree Test Instances)

Ephemeral, full-stack Kiro Crew gateways — one per feature worktree — that run on
their own port + isolated `KIROCREW_HOME` and never touch the live `:5476`
gateway or shared data. Linux `systemd --user`, macOS `launchd`, Windows Task
Scheduler. `<wt>` is a worktree name
(resolved by directory basename or `feat/<name>` branch convention).

| Command | Description |
|---------|-------------|
| `kirocrew pod install` | Lay down the systemd --user template unit (once per machine; Linux only, a no-op elsewhere) |
| `kirocrew pod provision <wt>` | Build the worktree's venv + SPA dist (the on-ramp) |
| `kirocrew pod up <wt>` | Bring up an isolated pod (auto-builds venv; fails if dist missing) |
| `kirocrew pod up <wt> --provision` | Provision (venv + dist build) then bring up |
| `kirocrew pod up <wt> --json` | Bring up and print `{base_url, token, port}` as JSON |
| `kirocrew pod ls` | List running pods |
| `kirocrew pod status <wt>` | Up/down + health for one pod |
| `kirocrew pod token <wt>` | (Re)mint a dashboard token for a running pod |
| `kirocrew pod url <wt>` | Print the pod's base URL |
| `kirocrew pod logs <wt> -n N` | Tail the pod's journal |
| `kirocrew pod down <wt>` | Evict the pod and delete its isolated HOME |
| `kirocrew pod restart <wt>` | Restart a pod in place, keeping its home; `--memory` and `--cpu` apply new ceilings (`--json`, `--wait-secs`) |
| `kirocrew pod prune` | Bulk-reclaim orphaned pod HOMEs (`--older-than 3d` by default, `--all`, `--dry-run`, `--json`) |
| `kirocrew pod scenarios` | List the seed scenarios `pod up --seed <scenario>` accepts (`--json`) |
| `kirocrew pod exec <wt> -- <args>` | Run a kirocrew command against a pod, using the pod's own binary and data |
| `kirocrew pod api <wt> <METHOD> <path>` | Call a running pod's HTTP API with its own token; prints `{name, method, path, status, ok, body}` |
| `kirocrew pod api <wt> POST config --data '{…}' --allow-write` | GET and HEAD are permitted by default; every other method needs `--allow-write` |

**Platform:** Linux (`systemd --user`), macOS (`launchd`; no memory/CPU ceiling)
and Windows (Task Scheduler; no restart on crash, `pod api` unsupported). On a
host with none of those, every service-manager verb refuses with a one-line
message pointing at `./dev-backend.sh` — it does not crash, and `pod install`
writes no unit file. `pod url` works anywhere (pure computation).

Port derivation: `base + (cksum(name) % 199) + 1` (base `7810` → `7811..8009`).
Override with `PORT=` in `~/.kiro/crew/pods/<name>.env`.

`kirocrew pod --help` lists every verb and its flags.

## Browsing (`browser` MCP tool)

Browsing is not a `kirocrew` subcommand. It is the **`browser` MCP tool**:
`op=navigate|back|snapshot|read|click|drag|scroll|type|fill|paste|press_key|dispatch_key|hover|select_option|upload|wait_for|wait_for_element|wait_for_network_idle|console|screenshot|tabs|dialog|reflex|js|cdp|http_get|iframe_target|ask`.
It drives one browser the gateway owns (the user's own Chrome where there is
one, Edge on Windows without Chrome, otherwise Kiro's headless Chromium), and the
user watches the page live in the chat, where one click takes over. It opens
public `http(s)` URLs only, plus loopback origins the user listed under
**Settings → Browser → Local preview origins**.

| Op | Description |
|----|-------------|
| `navigate {url}` | Open a page (returns URL and title) |
| `snapshot` | The page as an outline with element refs (`e5`); re-run after every page change |
| `read {ref?, offset?}` | Read the page's text, not only the screen; `ref` scopes it to one element and a long page names the next `offset`. An unselected tab or a closed section (`details`) is left out, so click the tab or open the section first. |
| `click {ref \| x,y}` / `type {ref?, text, submit?}` | Act on an element from a snapshot; `type` refuses a password field. `type {ref?, steps: [{text} \| {key, modifiers?}]}` runs ordered text and key steps in one call |
| `paste {ref?, text, html?}` | Put rich content (a table, headings, lists) into the field as a clipboard paste would, without touching the user's clipboard; refused where `type` is |
| `upload {ref \| x,y, path, via?}` | Give the page local files: `path` is one absolute path or a list of up to 10, each a file, a folder or a glob (at most 100 files, 25 MiB in all). `via`: `input` (default) for a file field the snapshot marks `[file]`, `chooser` for the file picker a button opens, `drop` for a drop zone; a folder goes only onto a drop zone. It asks on a card unless the user trusted acting on that site |
| `screenshot` | Write a PNG, return its absolute path; show it with `![..](../path)` |
| `tabs {action: list\|switch\|new}` | The tabs the agent opened, and only those |

**Browsing workflow:** load the `web-browse`, `web-verify`, or `browser-auth`
skill for the task. A sign-in, CAPTCHA or 2FA is the user's step: use the `ask`
op (the tool's description lists its args), or ask them to take over the live
view; your next op waits until they hand it back, then runs.

**Setup:** **Settings → Browser** installs the engine on the gateway's machine
and, on a desktop, connects the user's own Chrome (enable
`chrome://inspect/#remote-debugging` once, then Allow). A tool reply that
browsing is not set up points there; a `capabilities.browse` policy denial is
final. The old Playwright CLI install is gone: `kirocrew doctor` reports a
leftover prefix without touching it.
