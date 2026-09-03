# Computer Use Module (native desktop GUI automation)

Lets the agent **read and drive the operator's own desktop applications** through
the platform accessibility layer: enumerate on-screen apps, walk one app window
into an indexed accessibility tree, then act on an element by index (press,
type, set a value, scroll, perform a named action) or — for UI that exposes no
addressable element — on a screen point (click, drag). macOS and Windows both drive the
full tool set, differing in how input is delivered rather than in what is
supported (see [The Windows driver](#the-windows-driver)); Linux reports a typed
refusal.

Two things this module is NOT, stated up front because both are load-bearing
product decisions rather than implementation gaps:

- **Element-targeted, non-pointer input is the DEFAULT and the only thing
  available out of the box.** `computer_click` with an `element_index` performs
  `AXPress`, which activates a control with no pointer involved at all, and it is
  what `auto` picks whenever an index is present. Coordinate clicking and
  `computer_drag` are available for canvases, maps and custom-drawn UI, and they
  too leave the physical cursor alone — `click_method: "app_post"` posts a
  *located* mouse event to the target process with `CGEventPostToPid`.

  The one path that warps the real cursor is `click_method: "global"`. It has to be
  NAMED by the model — `auto` never resolves onto it — and every use emits a SEL
  record under its own `tool_kind`, so the pointer stays where the operator left it
  unless the model explicitly asked otherwise. Earlier revisions of this document
  asserted flatly that "the pointer never moves"; the accurate statement is that it
  never moves *by accident*. See
  [Coordinate clicking, drag and the real-pointer path](#coordinate-clicking-drag-and-the-real-pointer-path).
- **It is off until the operator turns it on**, out-of-band, in a file the agent
  can neither read nor write. See [The keystone primary enable](#the-keystone-primary-enable).

Two optional, human-facing views ride alongside, neither of which grants the agent
anything: the [live view (PiP)](#the-live-view-pip) mirrors screenshots the model
already read, and [Cursor Motion](#cursor-motion-a-real-desktop-overlay-cosmetic-only)
draws a fake cursor on the real desktop so a watching human can see where a click is
about to land. Cursor Motion is **not** the pointer and is deliberately invisible to
`screencapture`.

> Prior art: the tool surface and the per-turn element-index discipline are
> modelled on the open-source `open-codex-computer-use` MCP contract (MIT),
> which we probed to validate the shape. No code is derived from it — the driver,
> the compression pipeline and the security floors are all KiroCrew's own.

---

## Architecture: thin shim, in-gateway dispatch

```
kiro-cli
  └─ spawns  kirocrew mcp-computer            (stdio MCP server: kirocrew-computer)
       │      THIN SHIM — resolves session identity strictly, forwards, returns text
       ▼
     POST /api/computer-use/invoke            (loopback, X-Internal-Secret)
       │
       ▼  GATEWAY PROCESS
     dashboard/handlers/computer_use.py
       └─ computer_use/service.py  ── the ONE dispatch chokepoint
            1. enable_state.is_enabled()                  keystone primary enable
            2. computer_use/policy.py::check_app           target policy (self + operator lists)
            3. index freshness (TTL) + fingerprint re-walk
            4. policy.check_input_target                   secure field / text scan
            5. computer_use/gate.py::require_computer_use  SEL audit (no decision)
            6. ComputerUseBackend  ──►  macos_driver → macos_ffi (ctypes)
            7. re-snapshot, policy.redact_result
```

### Why the stdio process is a thin shim and the work happens in the gateway

The MCP stdio process does **no** accessibility work and **no** auditing. It checks
the keystone enable (so a disabled feature advertises zero tools), POSTs to the
gateway over loopback with the `X-Internal-Secret` handshake (the pattern
`mcp_core.py` already uses — the same `.local_secret` file; `X-Local-Secret` is its
sibling header, read only by `GET /api/token/local`), and relays the text result
back. Everything of consequence happens in the gateway.

### The shim is not spawned at all unless it can be used

`agent._computer_use_spec_gate()` decides whether `kirocrew-computer` appears in
the **emitted agent spec**: the keystone enable **and** a supported driver, or no
entry. This is a separate control from the two in-process checks, and it has to be,
because those run inside a process the spec already caused kiro-cli to spawn — they
can make a disabled feature advertise zero tools, but not make it cost nothing. It
cost ~109 MB of resident memory per chat process (including every `spawn_run`
subagent), and on a platform with no driver it cost that for a capability that could
never have done anything at all.

The support half asks **which platforms have a driver at all**
(`backend.platform_could_be_supported()`), never an inline `IS_MACOS`. That
distinction is what kept the server out of the spec on Windows after the Windows
read-path driver shipped: a hardcoded OS check left `@kirocrew-computer` in `tools`,
so the model was told the tools existed while no server was ever spawned to serve
them. `test_the_gate_names_no_operating_system` pins the predicate against that
regression.

The predicate is deliberately the CHEAP, optimistic half: it reads
`platform_compat` flags against `_SUPPORTED_PLATFORM_IDS` and answers "a driver
exists for this platform", not "it works on this host". It is **not**
`get_shared_backend().status()`, which would import the platform driver plus five
`WinDLL` loads (measured 31 ms and 32 modules on Windows) on a boot path, for an
answer the in-process checks make again anyway: `enable_state` runs in `_list_tools`
and again in the dispatcher, inside the process that would have done the work, so a
host whose driver turns out not to load still refuses — it just refuses there
instead of here. Never use this predicate to decide whether an ACTION may proceed;
for that `status()` is the only honest answer.

The keystone is tested **first** because both conditions must hold and both fail
closed, so the order is free to be the cheap one: `is_enabled()` is one small JSON
read, and it makes even the flag comparison unnecessary when the operator has not
opted in.

The gate **fails closed** — a missing, unreadable or malformed keystone yields no
entry — matching `enable_state`'s own posture, for a stronger reason: the open
position hands out the operator's whole desktop.

Both in-process checks stay, and they cover what the gate structurally cannot: the
keystone flipping **off** mid-session, after the spec was written and the backend
spawned. The gate covers what they cannot: the process existing.

`tools` is **not** touched. The `@kirocrew-computer` ref the shipped
`defaults.json` grants stays where it is: a ref resolves against the agent's own
`mcpServers` plus the global `mcp.json`, so once the entry is withheld the ref names
nothing and launches nothing. Removing it would destroy a mount the user may have
narrowed to a single tool and cannot be reconstructed on re-enable — the bare ref the
template re-adds is wider than what they chose.

The entry's `autoApprove` and user `env` keys are the one thing an off/on cycle does
reset. Stashing them would need a sidecar the agent can write, and an approval
restored from there would never reach the PreToolUse gate — see
[Managed servers](../../architecture/mcp.md#managed-servers). The operator
re-applies them. Pinned by
`test_computer_use_registration.py::TestSpecEmissionGate`,
`::TestGatedEntryIsNotPreserved` and `::TestGatedRefsAreLeftALONE`.

Three reasons the split is worth the extra hop, none of them governance:

* **the native work must not run in the shim.** A ctypes fault is not catchable in
  Python. In the gateway it is contained by the driver's `_guarded` seam and the
  bounded `subprocess_executor` pool; in a short-lived stdio child it would take the
  child down mid-call with no result to relay;
* **the snapshot cache has to be shared.** Element indices only mean anything
  against the walk that produced them, and that cache (`index.SnapshotIndex`) lives
  in the gateway. A per-shim cache would make every follow-up action refuse;
* **the audit belongs where the action happens.** SEL is a gateway service, and the
  trail is now the operator's primary record of what the agent did to their desktop.

**The shim MUST be told the data home.** Both processes read the keystone
`computer_use.json`, and a child does **not** inherit the gateway's
`KIROCREW_HOME` — the managed spec's `env` map is the only channel, so
`agent._managed_mcp_env()` pins it there (for every managed server, not just this
one). Without the pin the two sides read DIFFERENT homes, and the failure mode is
worse than a plain error because it is silent and self-contradictory: Settings writes
`enabled: true` to the override home, `mcp_computer` reads `false` from the default
one and publishes an empty `tools/list`, so the panel shows the feature ON while the
agent truthfully reports it has no computer-use tools. Both are telling the truth
about different files. Found by running the feature under `KIROCREW_HOME` in dev mode.

The pin is resolved through `paths._valid_override_home()` rather than reading the env
var, so an override the loader itself REFUSES is not handed to a child that would then
disagree in the other direction — the guarantee is *agreement with the gateway*, not
validity. It is refreshed like `command`/`args` (not preserved like `autoApprove`,
which is a user customization): a config written under an override and later refreshed
on a default install has the stale key REMOVED. A default install emits no `env` at
all, so the spec is byte-for-byte unchanged there. Pinned by
`test_computer_use_registration.py::TestDataHomePin`.

**Session identity.** The shim resolves it with
`mcp_core._resolve_session_key_strict()` — the gateway-injected per-call caller
block first (the shim advertises `kirocrew.caller-identity`, so gatewayd injects
one whenever it can name the caller — the only source that holds on a pooled
backend serving many sessions), else the env `KIROCREW_SESSION_KEY`, else
`KIROCREW_HOST_PID` plus the HMAC sidecar signed with the keystone-protected
`sel_hmac.key`. It is used for the audit record and for the live-view relay's
attribution, **not** as an authorization input: an unresolved key does not refuse the
call, because there is no per-surface ceiling left for it to select.

That is enforced in the SHIM as well as the gateway, and it has to be: neither
accepted source exists for a GUI-launched kiro-cli on **macOS**, the only platform
with a driver. `KIROCREW_SESSION_KEY` reaches a child only from a launcher that
already knows which session it is spawning for — the ACP spawn path
(`acp/client.py`) and the script-cron launcher (`cron_script.py`, which spawns one
process per job under `cron:<job id>`) — and `KIROCREW_HOST_PID` only from the Linux
sandbox launcher (`sandbox.py:666`). A GUI-launched kiro-cli has no such launcher
above it, so it carries neither. An earlier revision refused in the shim on the
reasoning that an unproven key is indistinguishable from an unattended surface —
with the unattended rule gone, that left the feature returning *"the calling
session could not be identified"* for every ordinary dashboard chat on macOS.
Found by using it.

The STRICT resolver is still the one called: the lenient variant walks a file
`mcp_core` documents as "agent-writable and therefore forgeable", and an empty audit
identity is honest where a forged one is a lie. The cost is attribution, not a
control. `test_mcp_computer.py::test_the_shim_carries_no_identity_refusal_at_all`
guards the absence, because a behavioural test alone passes just as well with a
refusal that happens to be unreachable.

**An unresolved key becomes `unresolved:<shim pid>[#<connection nonce>]`, never the
empty string** — and
that is a correctness fix, not cosmetics. `SnapshotIndex` namespaces entries by
`(session_key, window_key)`, so an empty key collapsed EVERY unresolved session onto
one `("", window)` slot. Since unresolved is the normal case on macOS, two concurrent
sessions observing the same window overwrote each other's element indices — and each
one's own `verify_fingerprint` still passed, because both trees describe the same
window, so the wrong-target action had nothing reporting it. kiro-cli spawns one shim
per session, so the shim's own pid separates the namespaces exactly as far as the
sessions are genuinely separate — in the 1:1 shim topology. On a POOLED backend one
process serves many sessions, so the pid separates only what the injected caller
block does not already name: co-tenants gatewayd CAN name get real per-session keys,
and the ones it cannot are separated by the **per-connection nonce** gatewayd injects
alongside the caller block (`_meta."kirocrew.tenant".nonce`, #5322). The nonce is
gateway-minted and stripped on every inbound frame like the caller block, so a stub
cannot choose to share a peer's namespace; it carries no session key, so
`CallerContext.from_meta` still finds no identity and nothing is laundered into
attribution. Both halves are read at call time rather than
captured at import, so a forked child cannot inherit its parent's string and
re-alias with it.

Absent nonce is the normal 1:1 case (no gateway, or a backend that never advertised
the caller extension), and the key stays `unresolved:<pid>` — correct there, because
one process is one session. It is ALSO what a pre-nonce gatewayd produces: `manager.py`
adopts whatever healthy daemon already holds the socket, so a daemon that outlived a
package upgrade keeps serving and injects nothing, and the backend cannot tell that
apart from the 1:1 case (absence of both blocks is ambiguous by construction). That
window is why `kirocrew-computer` stays in `_MANAGED_SERVERS_ADVERTISING_BUT_WITHHELD`
rather than being reclassified shareable here — the classification is a config WRITE
via `seed.py`, so it must not promise a separation the serving daemon may not
implement. A stub that reconnects gets a fresh nonce and therefore a
fresh namespace: its earlier snapshots become unreachable, which surfaces as "call
`computer_get_state` first" rather than as an action against a stale tree.

The prefix is deliberate: this is a namespace separator, not attribution, and an audit
reader must not mistake a pid — or a nonce — for a resolved identity. And it is a
namespacing fix
specifically **because** the alternative — refusing an empty key — is the line that
made the feature unusable on macOS; the security posture is unchanged, only the cache
key is.

### Why a separate MCP server rather than folding into `kirocrew-core`

`config/defaults.json` blanket-allowlists `@kirocrew-core` in `allowedTools`.
Riding inside it would inherit that blanket auto-approve for `computer_click`. A
separate slash-free server key also lets a fleet deny `@kirocrew-computer` with
one `mcp`-scope pattern. `@kirocrew-computer` is added to `tools` but
deliberately **not** to `allowedTools`, and the managed server spec carries **no
`autoApprove` key** — an autoApproved MCP tool is approved locally by kiro-cli,
emits no permission request, and never reaches `hooks.on_tool_call`.

### The backend seam and the shipped fake

`ComputerUseBackend` (`backend.py`) is a plain `abc.ABC` with a runtime-swappable
factory (`register_computer_use_backend` / `get_shared_backend` /
`reset_shared_backend`) — the shape `embeddings.register_embedding_backend`
already uses. It is deliberately **not** a `PlatformContext` extension point:
CPP is the *edition* seam (standalone vs companion) and its `CONTRACT_VERSION` is
pinned at 1, while computer use varies by *operating system*; a registry also
stays swappable inside a single pytest process. CPP still owns
`redact_via_context` (so a loaded companion's extra credential patterns apply to
computer-use output); the ABC owns **who acts**.

Contract every implementation honours: never raise (every failure becomes
`DriverResult(ok=False, text=…)`), **never move the pointer unless the request
says to** (only a `ClickRequest`/`DragRequest` whose `moves_pointer` is True, which
the chokepoint only builds after both permits cleared — a driver must never
upgrade a method on its own), set `ElementRec.secure` from BOTH role and subrole,
stamp `Snapshot.captured_at` from `time.monotonic()`, be thread-safe. `UnsupportedBackend` is a concrete shared base whose every method
returns the same typed refusal, so a new unsupported platform is ~10 lines and
cannot accidentally implement half a driver (`windows_driver.py`,
`linux_driver.py`).

`kiro_crew/testing/fake_computer_use.py` ships a `FakeComputerUseBackend` in the
runtime wheel (alongside `fake_acp_backend.py`), so a downstream suite can drive
the whole stack with no framework, no window, no application and no permission
grant. Its fixtures exist to make the security branches *reachable*: a node with
`role="AXTextField"` + `subrole="AXSecureTextField"` and a populated value, a
node whose title carries a credential-shaped literal and an exfil-shaped URL, a
blocked (terminal) app in the catalog, and a real decodable 1x1 JPEG. The suite
registers it process-wide; combined with the structural guarantee that no module
in the package calls `CDLL` at import scope, CI can never touch the native path.

Importing `kiro_crew.computer_use` is side-effect free: no framework load, no
file read, no platform branch until `get_shared_backend()` is called.
`select_default_backend()` is the ONLY platform branch in the package and it asks
`platform_compat.IS_MACOS` / `IS_WINDOWS` / `IS_LINUX`, never `sys.platform` —
which is also what lets a Linux runner exercise the Windows degradation path by
flipping one flag.

---

## The 11-tool contract

Server `kirocrew-computer` (slash-free: kiro-cli splits an agent `@server`
reference on `/`). All tools prefixed `computer_` so the GUI plane is
namespace-distinct from every other server's tools.

| Tool | Required | Optional | Class |
|---|---|---|---|
| `computer_list_apps` | — | — | observe |
| `computer_launch_app` | `app` (≤128) | — | mutate |
| `computer_get_state` | `app` | `text_limit` (1..20000, d=500), `max_tree_nodes` (1..5000, d=1200), `max_tree_depth` (1..128, d=64), `screenshot` (bool, d from config) | observe |
| `computer_click` | `app` + **exactly one of** (`element_index` \| `x`+`y`) | `click_count` (1..3, d=1), `mouse_button` (`left`\|`right`\|`middle`, d=left), `click_method` (`auto`\|`accessibility`\|`app_post`\|`sky_click`\|`global`, d=auto) | mutate, pointer |
| `computer_drag` | `app`, `from_x`, `from_y`, `to_x`, `to_y` | `mouse_button`, `click_method`, `steps` (1..512, d=1), `path` (`straight`\|`curved`, d=straight) | mutate, pointer |
| `computer_type_text` | `app`, `text` (≤10000), `element_index` | — | mutate, keyboard, text_entry |
| `computer_press_key` | `app`, `key` (≤64), `element_index` | — | mutate, keyboard |
| `computer_set_value` | `app`, `element_index`, `value` (≤10000) | — | mutate, text_entry |
| `computer_scroll` | `app`, `element_index`, `direction` (`up`\|`down`\|`left`\|`right`) | `pages` (0.1..20, d=1.0) | mutate, pointer |
| `computer_perform_action` | `app`, `element_index`, `action` (≤64) | — | mutate, pointer |
| `computer_end_turn` | — | — | control |

`computer_click`'s "exactly one of" is a CROSS-FIELD rule, so it is **not** in the
schema (`validate_tool_args` checks fields independently and has no vocabulary for
it) — it is enforced at the dispatch chokepoint by `policy.check_click_target`,
which the in-process entry point also traverses. Both failure modes are refused
rather than resolved by precedence: silently preferring the index would make a
model that meant the coordinates act somewhere else entirely, in a live
application, with no signal that it happened.

**`element_index` is REQUIRED on both keyboard tools, and that is a security control
rather than an ergonomic choice.** There is no "type into whatever is focused" form:
an unnamed target has no role or subrole, so `policy.check_input_target`'s always-on
secure-field refusal has nothing to inspect, and an indexless keystroke would type
into a focused password box. `computer_press_key` is included for a second reason —
`press_key("tab")` can *move* focus onto a password field, and the following keystroke
would land there. `computer_click` is the only element-scoped tool that accepts an
alternative, and only because coordinates are a target it can check
(`policy.check_click_target`'s one-of). **Three layers have to agree, and all three
now do:** `MCP_COMPUTER_SCHEMAS` (the enforcement point), the advertised
`inputSchema` in `mcp_computer._list_tools`, and `_ELEMENT_REQUIRED_TOOLS` at the
chokepoint. The validator gave both keyboard tools the OPTIONAL field spec — the one
that exists for `computer_click`, which legitimately takes coordinates instead — so
an indexless call passed validation and was refused one step later, and the comments
on both sides then described the *other* layer's behaviour (the chokepoint's called
itself "unreachable through the MCP path"). Enforcement held throughout, because that
`ValidationError` is converted to a refusal by `dispatch_tool`, but the chokepoint
check is kept as the last line of defence rather than deleted as redundant.
`SKILL.md`'s tool table states the same thing, since an optional-looking argument
there would have the model discover the refusal by hitting it.

Every tool has a `MCP_COMPUTER_SCHEMAS` entry in `validation.py`. That is
mandatory, not tidiness: an unregistered tool's arguments pass RAW through
`_validate_args`, and a `ValidationError` raised inside a handler escapes the
stdio loop and kills the server.

`computer_list_apps` and `computer_get_state` are the observation tools;
`computer_end_turn` is control-plane (it drops KiroCrew's *own* cached snapshots
and touches no other application, so it is neither observe nor mutate). The
class labels above are the code-owned `governance._CU_ACTION_CLASSES` table —
see [governance.md](governance.md).

All three spool writers (`service._persist_image`, `capture_macos.persist_jpeg`
and `capture_windows.persist_jpeg`) name their files with `tempfile.mkstemp`, not
a millisecond timestamp. `_shot_lock`
serializes writers within one service instance but cannot serialize a second
PROCESS — the gateway, the CLI and the permission-probe child all spool into the
same `tempfile.gettempdir()` directory — so a timestamp-only name let two captures
in the same millisecond resolve to one path, the second truncating the first and
leaving its caller holding a screenshot of an application it never asked about
(a cross-capture pixel leak, reviewer finding). `mkstemp` also creates the file
`0o600` from the outset, so there is no window in which it exists world-readable
before `restrict_to_owner` runs. The timestamp stays in the *prefix* because the
ring trim orders by name. If descriptor adoption or the write fails, each writer
removes its invocation-owned partial frame before degrading; it never sweeps a
neighbouring capture owned by another call or process.

**`computer_list_apps` only omits an app it cannot name.** It used to carry a
per-app governance filter (`gate.app_is_disclosable` against the `computer_use.apps`
/ `.app_names` axes) because it is the one verb that names every application and
resolves no target, so `require_computer_use` could not see them. With those axes
gone the function survives as a shape check: an app with neither a bundle id nor a
display name is dropped because there is nothing to show. Terminals and password
managers now appear in the list like everything else.

### Result shape: text only, by construction

Exactly what `validation.build_tool_response` emits:

```json
{"content": [{"type": "text", "text": "…"}]}
```

Text only, capped at `MAX_RESPONSE_LEN`. **There is no `isError` field and no
image block** — an image block is not expressible on this transport, so
"tree-first, relay the screenshot as a path" is a property of the transport
rather than a policy someone can regress. An error is the literal string
`"Error: …"`; `mcp_shared.call_tool_with_logging` classifies that prefix as SEL
`outcome="failed"`, so the prefix is load-bearing.

Rendered body:

```
App=com.apple.finder (pid 1041)
Window: "Documents", App: Finder.

0 window "Documents"
  1 splitgroup
    2 scrollarea
      3 button "Back" [AXPress]
      7 textfield <secure>
[tree truncated at 1200 nodes]

Screenshot: /var/folders/…/kirocrew-computer-shots/shot-1769472013411.jpeg
  (1280x604 jpeg, 24.2 KB) — read it with the fs_read tool only if the tree is
  insufficient.
```

Every mutating tool returns the REFRESHED tree at the configured budgets, so the
model always acts against indices it has just been shown. Its response is
`"<detail>\n\nRefreshed state:\n<tree>"`, and **the two halves are redacted
separately — deliberately, and neither is optional.**

The tree half is redacted inside `render_tree`; the header was concatenated *after*
that pass, and `detail` is not our prose — every driver confirmation interpolates
app-supplied text (`_click_text` embeds `app.name`, the process name macOS reports,
which is attacker-controlled). A process named `Notes key=AKIA…` therefore put a raw
credential directly in front of a fully redacted tree. Fixed by redacting `detail` at
the interpolation.

Redacting the *joined* string instead would break screenshots: `render_tree` appends
its image note after its own redaction because the per-user temp dir contains a long
random segment that `redact_credentials`' bare-secret-key heuristic masks, so a second
pass would replace every screenshot path with a placeholder and the channel would
silently never work (verified live; see `render._render_image_note`). Hence: header
redacted on its own, already-redacted body passed through untouched. Both halves of
that rule are pinned by tests, the second one structurally — a mutating tool's refresh
walk is `want_image=False`, so no behavioural test in that file would notice a
"just redact the whole response" simplification breaking the read path.

---

## The keystone primary enable

The primary enable lives at **`~/.kiro/crew/computer_use.json`**, NOT in
`config.json`:

```json
{ "enabled": false, "allowed_apps": [], "extra_denied_apps": [] }
```

Why not `config.json` — verified, and with a precedent in this repo:
`is_sensitive_write_path("~/.kiro/crew/config.json")` is `True` (the tool path is
protected) but `is_sensitive_bash_command("echo x > ~/.kiro/crew/config.json")` is
`None` and `is_denied(...)` is `None`. `security.py` states the governing
precedent outright: the denied-command opt-out is deliberately kept OFF
`config.json` **because it is a security ceiling**. A primary enable for full
desktop observation plus input synthesis is the same class of control, so
`computer_use.json` is added to `security._CREW_SECRET_LEAVES`, which gets
read+write protection on both the tool path (`is_sensitive_path`) and the shell
forms (`is_sensitive_bash_command`, including `cat`, `>`, `tee`, and
`tar -C`/`unzip -d` extraction into the trust root).

Mechanics:

- Every read fails soft to `{}` → **DISABLED**. Absent, unreadable, truncated or
  hand-mangled must never mean "enabled".
- `is_enabled()` is strict identity against `True`: a hand-edited
  `"enabled": "false"` (a truthy string) or `"enabled": 1` does not enable
  desktop control.
- The only writer is the dashboard PUT handler, which does not route through the
  agent tool gate.
- `allowed_apps` is an optional narrowing (empty = everything not denied);
  `extra_denied_apps` can only ADD. There is deliberately no mechanism to remove
  a built-in denylist entry.

`config.json`'s `computer_use` section carries **display and limits only** —
`max_tree_nodes`, `max_tree_depth`, `text_limit`, `attach_screenshot`,
`screenshot_max_px`, `screenshot_jpeg_quality`. The absence of an `enabled` field
there is deliberate; see [config.md](config.md).

---

## HTTP surface

Four routes in `dashboard/handlers/computer_use.py`, and the split in their auth
models is the point.

| Route | Auth | Caller |
|---|---|---|
| `GET /api/computer-use/config` | cookie (browser) | Settings panel |
| `PUT /api/computer-use/config` | cookie (browser) | Settings panel |
| `POST /api/computer-use/invoke` | loopback + `X-Internal-Secret` | the stdio shim ONLY |
| `POST /api/computer-use/frame` | loopback + `X-Internal-Secret` | this gateway's own capture thread ONLY |

`invoke` is in `server._STRICT_INTERNAL_API_PATHS` — no cookie fall-through, and
non-loopback is denied outright. It is the entry point to accessibility reads and
input synthesis, so it is the one route where a cookie fall-through would be a
genuinely new attack path rather than a convenience. It is registered in
`_register_mcp_routes` so the headless `--slack-only` server exposes it too (kiro-cli
spawns the shim on both entrypoints). The config pair is deliberately NOT in that
set: it is browser-called, like the browser-config pair.

**`GET /api/computer-use/config` fails SOFT on a malformed keystone, and that is a
deliberate inversion of the action path.** `load_policy_config` raises
`PolicyStateError` on a present-but-malformed `allowed_apps` because coercing it to
the empty tuple would silently convert an operator's restriction into no restriction —
right for a dispatch, wrong for a read. Letting it escape `_snapshot()` turned the GET
into an HTTP 500, so a hand-edited keystone made *the only UI that can repair the
file* unreachable; the page has to render precisely because the file is broken. The
handler therefore falls back to an empty `PolicyConfig` and publishes `policy_error`,
which the panel renders as a warning naming the file — an empty allow-list otherwise
reads as "no restriction configured", the opposite of what the operator wrote. **The
ceiling is unchanged:** every dispatch still calls `load_policy_config` itself and
still refuses on the same value, so only the *rendering* degrades. A test asserts both
halves (the GET renders; the action path still raises).

`frame` is in the strict set for the same reason and registered in the same block
(a `--slack-only` gateway drives the desktop too; it simply has no owner sockets
to deliver to). Like `invoke`, it re-asserts BOTH the loopback check and
`request["internal_auth"]` inside the handler: the strict listing does not prove
the secret was checked (an absent header falls through to cookie auth, and
`local_only=False` reclassifies every strict path as "mixed"), so without that
assertion a caller holding only a dashboard cookie or an app-scoped token could
inject arbitrary frames into every owner window's live view. See
[the live view](#the-live-view-pip) below.

**`GET /api/computer-use/config`** returns `{enabled, supported, platform, reason,
max_tree_nodes, max_tree_depth, text_limit, attach_screenshot, screenshot_max_px,
screenshot_jpeg_quality, allowed_apps,
extra_denied_apps, permissions{accessibility,
screen_recording, responsible_hint}, limits{field: [min, max]}}`.

- `permissions` comes from shelling `kirocrew computer doctor --json`
  (`asyncio.create_subprocess_exec`, fixed argv, 5s timeout, one
  `test_spawn_audit.BENIGN_SPAWNS` entry). Degrades to `unknown` on timeout,
  non-zero exit or unparseable output, and reports `unsupported` off macOS without
  spawning at all. A timed-out child is killed — the panel polls every 5s while a
  grant is outstanding, so leaking one per poll would pile up.
- `limits` publishes the server's own ceilings so the panel's number inputs bound
  themselves rather than re-spelling them in TypeScript.

**`PUT /api/computer-use/config`** accepts any subset of `{enabled, allowed_apps,
extra_denied_apps}` (→ keystone) and the seven display/budget fields (the five
`_INT_LIMITS` knobs plus `attach_screenshot` and `cursor_motion` → `config.json`), and
returns the refreshed GET payload. Everything is validated before anything is
written.

**It is dashboard-browser-only, asserted in the handler** — `request["app"]` must be
empty, else `403` before the body is read. That gate is the write boundary the
keystone design rests on: `request["user"]` is truthy for an App-Kit token too, so
the cookie check cannot separate them, and an app whose manifest declares
`permissions.api: ["/api/computer-use"]` satisfies `app_token_path_allowed` (a bare
prefix matches on the path boundary). Since `enable_state.save_state` deliberately
bypasses `is_sensitive_path` — that is what lets the operator's own panel write a
file the agent cannot — an agent able to author an app manifest could otherwise read
its own `.app_secret`, mint a token, and flip `enabled: true` on its own desktop
automation. Same guard as `handlers/kiro_prerequisite.py` and `messaging.py`'s
notification push; the two machine routes are covered instead by their
`request["internal_auth"]` assertion, which an app token can never satisfy.

**There is no `409` and no `read_only` field**, because there is no ceiling: no
`computer_use*` row exists in `SCOPE_CATALOG`. An earlier revision of this spec (and
of the handler's own docstring and a 16-line comment) described a `409` for a widening
request under a forbidding ceiling and a `forbidden`-only `read_only` the panel would
key `disabled` on. Both belonged to the governance model that was removed by product
decision, and the prose outlived the code — there is no `status=409` in the module at
all. Do not re-document either without re-implementing it.

A corrupt keystone or `config.json` is `500` and is left byte-identical rather than
clobbered (`StateCorruptError`, the `ConfigCorruptError` precedent). Every mutation
SEL-audits the decision (`enabled=…`, changed field names) — never the app patterns
themselves, and the app-token refusal is audited too.

**`POST /api/computer-use/invoke`** takes `{tool, args, session_key, agent, app}`
and returns `{"text": …}` with a 200 for BOTH success and refusal, because a
computer-use refusal is a tool result (`"Error: …"`, which the SEL layer classifies
as failed) rather than a transport failure the model cannot reason about. Only a
malformed request gets a 4xx. The identity fields are not an authorization claim
this handler trusts — the shim resolved them strictly, and the fail-closed gate
treats an empty `session_key` as unattended and denies; the handler never infers
one. The dispatch runs in a worker thread (accessibility calls block for tens of
milliseconds).

---

## What is enforced, and what is not

One operator opt-in, then the agent drives the desktop the way the operator would.
That is a deliberate product decision and it replaced a much larger governance
model — eight `SCOPE_CATALOG` rows, an unattended-surface refusal, an
interactive-approval floor, a per-app disclosure filter and a five-category app
denylist. Those are **gone**; this section is the honest replacement.

### The dispatch chokepoint, in order

`tools.py::_dispatch` is still the single funnel every tool passes through, and the
order still matters, but there are only four steps left:

1. **Schema validation** — `validation.MCP_COMPUTER_SCHEMAS`. An unregistered tool
   is refused before anything else, because unvalidated arguments reaching a handler
   would escape the stdio loop and kill the server.
2. **The keystone primary enable** — `enable_state.is_enabled()`. One read serves
   both the enable test and the operator's target lists, so a hand-edited file
   cannot be observed in two states within one dispatch.
3. **OS identity resolution** — the window list only. The app the driver *resolved*
   is what gets audited, never the agent-supplied `app` string.
4. **The target policy** — `policy.check_app` (the one retained denylist entry plus
   the operator's own allow/deny lists) and `policy.check_input_target` (the
   secure-field refusal).

`gate.require_computer_use` still sits at step 4's boundary and still has the same
signature, but it now only **audits**: there is no governance decision left to make.
Keeping the call (rather than deleting it) is what keeps the audit trail complete
and gives a downstream edition one place to reintroduce a decision.

### What still refuses

| Refusal | Where | Why it survived |
|---|---|---|
| Feature off | `enable_state` + `tools` step 2 | The keystone is on `security._SENSITIVE_HOME_DIRS`, so the agent can neither read nor write it. This is the whole security model now. |
| KiroCrew's own window | `policy.check_app` | Driving our own Settings UI would let the agent click the enable above — a self-escalation loop that would make the keystone pointless. Matched on bundle id, process name **and window title** — see below. |
| Password fields | `policy.check_input_target` + `render` + `capture_macos` | A privacy floor, not a policy knob: the value is never read, never rendered, and a window holding one is never photographed. |
| Operator's own app lists | `policy.check_app` | `allowed_apps` / `extra_denied_apps` on the keystone. The operator's choice, not a shipped ceiling. |
| Stale / drifted element index | `index` + `service.verify_fingerprint` | Correctness, not authorization — acting on a stale index clicks the wrong control. |
| Credential-shaped output | `policy.redact_result` | The repo-wide egress control every other surface already runs. Applied to the action HEADER as well as the tree — the header interpolates app-supplied text (a process name), so it was an unredacted egress path on its own. |
| Indexless keyboard input | `tools._ELEMENT_REQUIRED_TOOLS` | `computer_type_text` / `computer_press_key` REQUIRE `element_index`. An unnamed target has no role or subrole, so the password-field check above has nothing to inspect and the keystroke would land in whatever the app happened to have focused. `press_key("tab")` is included because it can *move* focus onto a password box. An earlier draft of this document listed this under "no longer refuses"; that was never implemented, and the doc was the thing that was wrong. |
| Non-left `sky_click` | `policy.check_method_button` | The private recipe is a left-button sequence. Refused rather than downgraded, because synthesizing a left click for a right-click request performs a different gesture than the one asked for. |

### What no longer refuses

Stated plainly, because these are behaviour changes a reader will otherwise trip
over:

* **unattended surfaces** — cron, subagent, taskrunner, webhook, workflow and
  channel sessions all drive the desktop. There is no `UNATTENDED_SURFACES` rule;
* **terminals, password managers, System Settings and system auth dialogs** — all
  readable and drivable. The shipped denylist that covered them was incomplete by
  construction (an IDE's embedded terminal was never matched) and got in the
  operator's way on their own machine;
* **the real-pointer path** — `click_method: "global"` needs no second opt-in and no
  governance permit. It still has to be NAMED by the model (`auto` never resolves to
  it), so the cursor is never warped by accident, and every use is audited under its
  own `tool_kind`;
* **paste** — `cmd+v` is allowed;
* **observation channels** — `apply_observation_ceiling` is a pass-through. Window
  titles, element values and file paths are not narrowed;
* **interactive approval** — there is no `computer_use.approval` row, so nothing
  makes the feature observation-only.

### Accountability replaces authorization

With the ceiling gone, the SEL audit trail is what the operator has. Every call is
recorded (`gate._audit_allowed`), every refusal is recorded (`tools._refusal` /
`_static_refusal`), and a real-pointer gesture gets its own `tool_kind`
(`computer_use_pointer`) so "did the agent ever take control of my mouse?" is one
filter over the log rather than a parse of every row.

## Launching an application (`computer_launch_app`)

The tool set drove windows that already existed, so the desktop the agent could
reach was whatever the operator happened to have open. `computer_launch_app` opens
one. It is the **only verb in this package that creates a process**, which is why
its target resolution is the narrowest thing here.

### The catalog is untrusted input; the verified target is the guarantee

The obvious design — "ask the OS what this app is, then run it" — is unsafe on
Windows, and the reason is four measurements rather than a theory:

| Resolution input | Writable by the agent's own user? |
|---|---|
| `HKCU\…\CurrentVersion\App Paths` | **yes** (verified: created a key aimed at another binary) |
| `%LOCALAPPDATA%\Microsoft\WindowsApps` — **and it is on `PATH`** | **yes** (34 execution aliases live there) |
| per-user Start Menu (`%APPDATA%\…\Start Menu`) | **yes** |
| `%ProgramFiles%` / `%SystemRoot%` — the *variables*, not the directories | **yes**, trivially: they are ordinary environment variables |
| `HKLM\…\App Paths`, `HKLM\…\CurrentVersion`, `C:\Windows`, `C:\Program Files`, `C:\Program Files\WindowsApps`, `%ProgramData%\…\AppRepository` | no |

So `shutil.which("mspaint")` resolves to
`%LOCALAPPDATA%\Microsoft\WindowsApps\mspaint.EXE` — an **agent-writable path**. A
launch verb resolving a name that way would run whatever the agent planted, and
because computer use is deliberately not governance-gated that binary would also
skip the `BUILTIN_DENIED_RULES` floor every `bash` call passes.

A "trusted catalog only" design does not work either: **no fully-protected source
enumerates a packaged app's AUMID.** `C:\Program Files\WindowsApps` is protected but
*not listable* (`os.listdir` → `WinError 5`), the machine-wide
`Windows.Launch\PackageId` key holds only system components, and the
`StateRepository` database is unreadable. Paint — the canonical drawing target — is
a packaged app, so a protected-catalog-only resolver reaches nothing worth reaching.

`launch_windows` therefore reads **every** catalog including the writable ones, and
then verifies what came back:

1. **the resolved executable must sit under a root this user cannot write, AND its own
   containing directory must not be writable either**, tested after
   `os.path.realpath` so a junction cannot borrow a protected prefix.

   The second half is not redundant, and the reason is measured: **an unwritable root
   can contain writable descendants.** On one host `C:\Windows\Temp`,
   `C:\Windows\Tasks`, `C:\Windows\System32\spool\drivers\color` and
   `C:\Windows\System32\Microsoft\Crypto\RSA\MachineKeys` are all writable by an
   unprivileged user while their parents are not — so a prefix test accepted a planted
   `C:\Windows\Temp\Evil.exe`, named by an `App Paths` entry in the writable `HKCU`
   hive. `C:\Windows` is consequently not a root at all (`System32` comes from
   `platform_compat._windows_system_dirs()`, which names it specifically), and the
   file's directory is probed with a real create-and-delete rather than an ACL
   computation — an effective-permissions model has to account for group membership,
   inherited denies and elevation, and a subtly wrong one fails OPEN. The
   reverse move — a junction *under* a protected root aimed at agent-writable
   content — was attempted and refused by the OS (`mklink /J` under
   `C:\Program Files` → "Access is denied"), which is what makes the prefix test
   meaningful in both directions.

   **The root LIST is read from `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion`, never
   from `os.environ`**, and that is the single most load-bearing line in the module. An
   earlier draft read `%ProgramFiles%`, which is an ordinary environment variable — so
   anyone able to set it could nominate a directory they can *write* as a "protected"
   root, and the verification then accepted a planted binary. Confirmed against that
   draft end to end: pointing `ProgramFiles` at a temp directory made
   `_under_protected()` answer True for a file written there, defeating checks 2 and 3
   below at the same time. `platform_compat._windows_system_dirs` already declines to
   trust `%SystemRoot%` for the same reason (it prefers `GetSystemDirectoryW`), and
   `System32` still comes from there. `test_the_protected_roots_do_NOT_come_from_the_environment`
   poisons all four candidate variables at once, so any one of them regaining influence
   is a red test;
2. **the basename must match the catalog key that found it.** An agent that rewrites
   a writable `App Paths` value can then only aim it at a file that already exists
   under a protected root *and* is named what the key claims — which reduces the
   rewrite to "start an app that is already installed under its own name". Measured
   over every real entry on one host: 46 of 47 resolvable entries satisfy it, the
   single exception being a deliberate Microsoft alias (`IEDIAG.EXE` →
   `IEDIAGCMD.EXE`), which is refused rather than special-cased;
3. **the argv is exactly `[executable]`.** No document, no flag, no URL — the MCP
   schema has one `app` field and nothing else. Any of those would turn "open an
   application" into "run a program with attacker-chosen input".

macOS needs less of this: an application is a `.app` bundle in a small set of
conventional directories and there is no `App Paths` analogue, so `launch_macos`
lists those roots and hands the verified bundle PATH to `/usr/bin/open -a` (pinned
absolute, never resolved from `PATH`). The path, not the name: `open -a <name>` asks
LaunchServices to resolve it again from its own database, which indexes bundles this
module deliberately excludes, so the bundle that was checked and the bundle that runs
need not be the same one.

macOS verifies its resolved bundle the same way, with the conditions that fit its
filesystem. The bundle must sit under one of the enumerated roots, and **`~/Applications`
is not one of them** — it is writable by the same user the agent runs as, so including
it would let a planted `~/Applications/Anything.app` be launched by name.

Then **every directory on the path to the code that will run is probed for writability**
(`_writable_component`): the bundle's parent, the bundle itself, `Contents`, and
`Contents/MacOS`. The parent alone was not enough, and the gap it left is the common case
rather than a corner. `/Applications` is root-owned and unwritable, but a bundle installed
there by any user-space installer — every drag-install, every Homebrew cask — is owned by
the installing user, so `Foo.app/Contents/MacOS/Foo` can be replaced without
`/Applications` ever being writable. A parent-only probe passed that bundle and launched
agent-authored native code. The parent probe still runs, which also catches an
`/Applications` that is group-writable on a machine whose admin group has been widened.

**Every level, not just the endpoints.** Write access to any single directory on that path
is enough to control what executes, and `Contents` is the one an endpoints-only check
misses: its writability is independent of both neighbours, and owning it allows
`mv Contents/MacOS Contents/MacOS.bak && mkdir Contents/MacOS`, which yields an agent-owned
executable directory that every other probe then reports as unwritable. It also holds the
`Info.plist`, so the same access rewrites `CFBundleIdentifier` and defeats the pre-spawn
identity deny above.

**Two permissions, so two tests — on BOTH platforms.** Directory writability governs create,
unlink and rename; rewriting an existing file's bytes needs write on the file itself and no
directory permission at all. A create-probe therefore answers "unwritable" for the *ordinary*
drag-install and Homebrew-cask shape — root-owned directories that deny creates, holding an
executable owned by the installing user — while `open(exe, "r+b")` replaces the binary in
place. A target has to fail both questions to be trusted:

**Every directory up to the protected root is probed on both platforms, not just the
immediate parent.** Write access to any single level is enough to substitute the code that
runs: an agent owning an intermediate `C:\Program Files\Vendor` renames its `App` child aside
and recreates it with its own binary inside, and a parent-only probe then reports the leaf it
just created as unwritable. `_any_directory_is_writable` walks from the executable's directory
up to and including the root and stops there — everything above is off the caller's path by
construction, since the prefix test already placed the target inside it. `launch_macos`'
`_writable_component` does the same for a bundle's four levels.

**And the VERIFIED path is what gets spawned.** `resolve_target` resolves once and returns that
`realpath`, not the raw catalog value: the two differ in the last component whenever a link or
an 8.3 alias is involved, so returning the raw one would hand `spawn_detached` a string whose
final component the verification never examined. `launch_macos` returns its verified bundle
path for the same reason.

**Ownership is the durable question on both platforms, and the current permission is not.**
A mode or an ACL that denies write today can be revised by the file's owner tomorrow, with no
privilege — so "write is refused right now" says nothing about whether it will be refused a
moment later, while "someone else owns this" does, because taking ownership needs a privilege
an ordinary agent lacks. Both platforms therefore ask ownership first and treat an
unobtainable answer as owned, so an unverifiable target refuses:

- macOS `_any_executable_is_writable` checks every file under `Contents/MacOS`, not just the
  one `CFBundleExecutable` names (that value is inside the bundle, so it cannot say which file
  matters), and rejects on **`st_uid == geteuid()`**. The owner may `chmod` at will, so a
  read-only mode on a file this user owns is a fact the same user undoes between the check and
  the `exec`. The mode is still checked, for a file owned by someone else that is group- or
  world-writable.
- Windows `_file_is_replaceable` is the same second condition in `_under_protected`. The owner
  of a Windows object holds `WRITE_DAC` implicitly, so an agent that plants a binary and then
  denies itself write can revoke that deny ACE again — measured, `icacls /deny` then
  `icacls /remove:d`, both unprivileged. `_owned_by_current_user` compares the file's owner SID
  against the process token's with two read-only Win32 calls, and its `ctypes` import is
  deferred into the function exactly as `winreg`'s is, so the module still imports on a Linux
  runner. This still admits the real catalog — `System32` binaries are owned by
  `NT SERVICE\TrustedInstaller`, and 44 of the 47 resolvable entries on the measured host
  still resolve (the three refusals being the shells and the `IEDIAG` alias).
  For a file owned by someone else it then uses **`os.open(.., O_RDWR)`**, never
  `os.access(.., W_OK)`: on Windows `os.access` reports the read-only *attribute* and never
  consults the ACL, so it answers True for every `System32` binary and would refuse the whole
  catalog — measured from an unelevated shell, `os.access` says writable for
  `System32\notepad.exe` while opening it for write is denied.

On macOS the **`Info.plist` is checked the same way**, because it is a launch *input* rather
than description: it supplies the `CFBundleIdentifier` that `target_identity` hands the policy,
so a plist this user can rewrite means a forged bundle id passes the pre-spawn deny under a
name the operator never blocked — and the post-launch check re-reads the same forged string.
An unwritable `Contents` does not protect it, by the same create-versus-replace argument one
file over.

Neither probe modifies anything (no `O_TRUNC`, no write, metadata reads only), because both
run on a target the launch is about to ALLOW and must not damage a real installed application.
Both fail closed: a file that cannot be examined counts as writable.

Those three locations are **fixed, never read from the bundle**. Asking the bundle's own
`Info.plist` where its executable lives is the obvious refinement and it is the wrong one:
`CFBundleExecutable` sits inside the very directory whose trustworthiness is in question,
and an absolute or traversing value aims the probe outside the bundle (`/tmp/x` probes
`/tmp`; `../../..` walks up out of it), letting the target choose which directory gets
judged. `Contents/MacOS` is the location the OS itself requires, and any deeper nesting
sits under a directory already being probed.

The cost is real and is the right trade: an application installed only for the current
user cannot be launched by name. The refusal says so and names the remedy (move the
bundle under `/Applications`, or open it by hand), which is recoverable — where a launch
verb that runs whatever the agent wrote is not.

A **command interpreter is refused by name** on both platforms (`cmd`, `powershell`,
`wt`, `Terminal.app`, …). That list is explicitly *not* a security boundary and does
not claim completeness — an IDE with an embedded terminal defeats it. It exists
because a shell is the one target the no-arguments rule cannot bound: it takes its
work from a subsequent keystroke, so launching one and then using
`computer_type_text` would reach a shell with none of the command-deny floor.

### A launch is confirmed by finding the WINDOW

`backend.await_launched_window` polls the on-screen window list; it does **not** read
the spawned process's exit status. That is not defensiveness — a launcher exits
before the application it started has a window. Measured on Windows,
`explorer.exe shell:AppsFolder\<AUMID>` returned `rc=1` after 2.9s while MS Paint's
window appeared at **9.9s**; `/usr/bin/open` returns as soon as LaunchServices accepts
the request. Either exit status read as the outcome would report a successful cold
start as a failure.

Three outcomes, and the third is the one that matters:

- **window found** → success, and the dispatcher **snapshots it in the same turn**. A
  fresh window has no cached indices, so without that snapshot the model's only
  possible next call is `computer_get_state` on the app it just launched;
- **already running** → *refused*, naming the window and pointing at
  `computer_get_state`. A second copy of an editor is a second unsaved document, and
  the model's actual goal is already met;
- **no window inside 30s** → **success with a caveat**, not a failure. The process
  really did start. Reporting failure is what makes a model launch again, and the
  second attempt is what produces two copies of the application.

### The policy is checked TWICE, and the second one is the real gate

`tools._dispatch` checks the caller's raw string before touching the catalog, and that
alone is not sufficient — **because a name resolves.** A prefix query reaches a different
application than the string that was checked: an operator who denied `notepad` was
bypassed by a request for `note`, since no rule matches `note` and the resolver then
returned Notepad. Verified end to end.

So the same predicate is handed to the driver as `permit=` and re-applied to the
**resolved** identity inside `backend.run_launch`, still before `spawn`. Two properties
make that the right shape rather than a patch:

- it is where the resolved identity first exists AND the last point before a process does,
  and a detached spawn cannot be undone;
- it fixes the defect without narrowing the resolver. Prefix matching is what makes the
  verb usable (`paint` → `mspaint`); the bug was never that a prefix resolves, it was that
  the policy was evaluated against a different string from the one that got launched.

Both checks build their `AppRef` through one helper (`tools._launch_probe`), because if
the two descriptions diverged a rule could fire on one and not the other — which is the
bypass again.

**The resolved check carries every spelling, not just the display name**, which is the
second half of the same defect. A launch is requested by display name (`notepad`) while
the identity the OS reports — and therefore the spelling every other computer-use refusal
prints, and the one an operator copies into `extra_denied_apps` — is `notepad.exe` or
`com.apple.TextEdit`. A check that knew only the display name matched neither, so
`extra_denied_apps: ["notepad.exe"]` let Notepad start and refused it only once it was
running. Each platform's resolver therefore supplies a `LaunchIdentity` (`launch_windows.
target_identity` → the verified executable's basename; `launch_macos.target_identity` →
`CFBundleIdentifier`), and `LaunchIdentity.as_app_ref()` puts both names in the fields
`check_app` reads them from.

The Windows key is the basename of the **resolved** path, never the raw catalog value. An
8.3 short name (present by default on the system volume) otherwise renames the target out of
the operator's rule: an agent-written `App Paths` value naming `…\SOMEVE~1.EXE` resolves and
is accepted — the basename rule compares `basename(realpath(…))` against the key — but
reporting the raw basename hands the policy `SOMEVE~1.EXE`, which `someverylongname.exe` does
not match. A file symlink does the same where one can be created.

`run_launch`'s `identity=` parameter defaults to `None`, and that default degrades to the
display-name-only check, so a driver that omits it silently restores the vulnerability with
nothing going red — measured: deleting it from both drivers left the whole launch suite
green, because every dispatch-level test runs through the fake, which supplies its own.
`test_EVERY_driver_forwards_its_platform_identity_supplier` pins both.

### A hosted app cannot be denied before it starts, only before it is driven

There is a third check — `run_launch`'s `refuse_launched`, applied to the `AppRef` the window
list reports once a window exists — and it covers what the pre-spawn one **structurally
cannot**. A packaged app's window is fronted by `ApplicationFrameHost`, so
`apps_windows._app_ref` publishes the window TITLE as both name and bundle id, because the
broker's image name identifies no application. That title is therefore the only spelling an
operator can write a rule against, and it does not exist until a window does: before the
spawn the catalog offers `store.exe` and nothing else. Measured on a real host,
`extra_denied_apps: ["Microsoft Store"]` matched neither pre-spawn identity.

The residual is stated rather than hidden: **a denied hosted app can be made to start once.**
What the check prevents is the launch reporting success — the driver returns a refusal
instead of an `AppRef`, so nothing snapshots or drives the window, and every subsequent verb
resolves the same title and refuses too. Closing it fully would need a pre-spawn map from
executable to package display name, and no protected source on the measured host enumerates
one (see `launch_windows`' module docstring on `WindowsApps` being unlistable).

**An operator's DENY pattern is matched against the window title too**, not only `name` and
`bundle_id` — `check_app` passes it and `_matches_operator_pattern` takes it as a fourth
argument. Without that, an app reporting its own executable while carrying a different title
(Snipping Tool → `SnippingTool.exe`) could not be denied at all: only the built-in floor read
`title_substrings`, so the operator had no spelling that matched. **The allow-list
deliberately does NOT read it.** A title is chosen by the application, and often by the
document it opened, so allowing on a title would let any app satisfy an allow-list by naming
itself after a permitted one — widening the one control an operator has for narrowing. The
asymmetry is the safe direction: a broadened deny refuses something recoverable, a broadened
allow does not.

Two limits remain, both worth knowing before relying on this:

- **it needs a window.** The no-window branch is a success with no `AppRef`, so there is
  nothing to check — a process that starts and shows nothing within the timeout is reported
  as started, whatever it turns out to be;
- **the pre-spawn check still only knows the catalog's spellings.** `extra_denied_apps:
  ["SnippingTool"]` refuses before any process exists; `["Snipping Tool"]`, with the space
  that only the *window* title carries, refuses after the window appears. Both refuse; they
  differ in whether a process was created first.

That is **one** `AppRef` rather than a check per name, and the allow-list is why: a
non-empty `allowed_apps` refuses any name absent from it, so refusing on each individual
miss would mean an operator who allow-listed one spelling was defeated by the other
failing the same list. Carrying both in one ref gives union-deny and either-matches-allow,
exactly as a running app is already evaluated.

### The denylist check is weaker here, and the spec says so

Every other verb resolves an `AppRef` from the window list first, so `policy.check_app`
sees a bundle id, a process name and a window title. A launch has only the name the
caller typed, so `tools._dispatch` synthesizes an `AppRef` from it. The self-target
rule — the one that keeps the agent out of Kiro Crew's own Settings — matches on name
**substrings**, so it does fire on a name-only ref; a hypothetical denylist entry
naming only a bundle prefix would not. What closes the rest is a **second**
`check_app` after the launch, against the identity the OS actually reported: an app
that got launched still cannot be read or driven.

The residual, stated rather than discovered: this lets the agent start an installed
application the operator did not ask for. That is the same posture as the rest of
computer use once the operator enables it (see [Known
limitations](#known-limitations)) — one opt-in, then the desktop — not a new plane.

## Coordinate clicking, drag and the real-pointer path

Element addressing is the preferred path and stays the default: `AXPress` activates
a control with no pointer at all, needs no pixel measurement, and is what `auto`
picks whenever an `element_index` is present. But some UI has no addressable
element — canvases, maps, timelines, custom-drawn controls — and some gestures have
no accessibility form at all. Hence a coordinate `computer_click` and a
`computer_drag`.

### Multi-point dragging: one `SendInput` call PER POINT

A drag used to send `[move(start), down, move(end)]` — exactly one intermediate
point — so it could express a slider sweep and a range selection but only ever drew a
**straight line**. `steps` (segments, d=1) and `path` (`straight` | `curved`) make a
stroke expressible; `cursor_motion.drag_points` computes the geometry, reusing the
Bezier + perpendicular arc the cosmetic overlay already uses so the drawn gesture and
the drawn cursor cannot diverge.

**On Windows, fidelity depends on the number of `SendInput` CALLS, not on the number
of records submitted.** Measured against MS Paint:

| Submission shape | Result |
|---|---|
| 65 move records in ONE `SendInput` batch | a coarse polyline, visibly flat-topped |
| 65 points as 65 separate `SendInput` calls | a smooth curve |
| the same, with 2ms / 8ms of inter-call sleep | identical to 0ms |

A `WH_MOUSE_LL` hook counted **65 of 65** `WM_MOUSEMOVE` in *every* shape, so nothing
is dropped at the queue. Windows synthesizes `WM_MOUSEMOVE` from queue **state** when
the target's message pump asks, rather than delivering one message per injected event
— so a single batch mutates the cursor position many times before the target samples
it once, while separate calls yield in between. There is deliberately **no sleep**: it
buys nothing measurable and costs wall-clock with the operator's mouse button held
(batched 0.046s, 0ms-paced 0.105s, 8ms-paced 0.656s for the same arc).

This reads as a contradiction of `windows_ffi._send`'s own docstring, which explains
why input is batched, so `test_each_path_POINT_is_its_own_SendInput_call` pins it: the
regression is invisible in code review because it produces a drag that still works and
no longer draws.

**macOS keeps its own interpolation for a default request.** `macos_ffi` already
synthesizes `DRAG_STEPS` (6) intermediate `MouseDragged` events, tuned live against
TextEdit — which needed them before it registered a selection at all. So the driver
passes a path only when the caller actually asked for one (`steps > 1` or a non-default
`path`); handing the FFI a two-point path unconditionally would have silently stopped
every existing macOS drag being recognised as a drag.

**Confinement widened with the path, and it had to.** Two authorized endpoints were
sufficient while a drag was a straight chord — a segment between two points inside one
rectangle stays inside it. A `curved` path bows AWAY from the chord by up to
`CURVE_AMOUNT_MAX` (110px), so a stroke between two authorized points near a window
edge can travel *with the button held* over the window beside it. Both drivers now
confine **every** point of the path.

### Image pixels to screen points

`SCREENSHOT_NOTE` says the image is downscaled and that coordinates must come from an
element's own frame. That is the right rule and it is unusable for the surface that
most needs pixels: a canvas, a map or a chart is a single element (or none), so no
frame names the point the model wants *inside* it. Without a conversion, "click the
middle of that shape" is inexpressible — which is what made drawing impossible rather
than merely awkward.

`render._render_scale_note` therefore publishes the arithmetic:

```
Image-pixel to screen-point conversion: screen_x = 200 + image_x / 0.500,
screen_y = 100 + image_y / 0.500 (the image is 0.500x the window's 1600x900 pixels).
```

Stated as a recipe rather than a bare ratio, because a model handed "scale 0.66" still
has to be told which direction to apply it and that the window origin is added
afterwards — getting either backwards puts the click on the wrong half of the screen.
Three decimals, because a rounded 0.66 against a 1920px window is a ~7px error at the
far edge. One scale rather than one per axis: the encoder preserves aspect ratio (it
scales by the long edge), and the ratio is taken from the WIDTH because a window's
height includes its title bar while there is no such ambiguity horizontally.

Emitted **only** when the window bounds, the encoded width and the window width are
all present and positive. A wrong conversion is worse than none: a model applying a
bad ratio clicks confidently in the wrong place, where a model given nothing falls
back to an element frame. A secure window publishes neither the image nor the
conversion — otherwise the suppression would leak the geometry it was suppressing.

### The click methods

| Method | Delivery | Moves the cursor? | Buttons | Needs |
|---|---|---|---|---|
| `accessibility` | `AXUIElementPerformAction(elem, "AXPress")` + the ladder, then the enclosing control | no | left (press ladder), right (`AXShowMenu` only) | `element_index` |
| `app_post` | `CGEventCreateMouseEvent` + `CGEventPostToPid` | **no** — verified live: the prototype's cursor position was identical before and after | all | `x`+`y` |
| `sky_click` | private SkyLight recipe (see below) | **no** | **left only** | `x`+`y`, a resolved window id |
| `global` | `CGWarpMouseCursorPosition` + `CGEventPost(kCGHIDEventTap)` | **YES** | all | `x`+`y`, and the model must NAME the method |
| `auto` (default) | resolves to `accessibility` when an index was given, else `app_post` | no | all | — |

**`auto` never resolves to `global` or to `sky_click`.** That is an invariant with
tests, not a preference: an implicit resolution onto the pointer-warping path would
let a model take the operator's mouse without ever naming the method, and an implicit
resolution onto the private path would put undocumented ABI on the default route.

**`sky_click` is left-button only, and a non-left request is REFUSED rather than
downgraded** (`ERR_SKY_CLICK_BUTTON`, gated at the chokepoint by
`policy.check_method_button` and re-checked inside `macos_skylight`). The recipe was
reverse-engineered for a left click and the button number is one field among nine, so
a right-click variant would be invented rather than observed. Downgrading was the
actual bug an earlier revision shipped: the recipe took no button at all and built
left-button codes unconditionally, so a right-click request silently *activated the
control* instead of opening its context menu — on a background window the operator
cannot see. Same reasoning as `AX_MENU_LADDER` never falling back to `AXPress`:
performing a different gesture than the one requested is worse than performing none.

### `sky_click` — the private path, and why it IS shipped

`sky_click` clicks a window that is **behind other windows**, without raising it and
without moving the pointer. It is the only method built on undocumented Apple ABI,
and an earlier revision of this feature deliberately did NOT port it on the grounds
that a shipped product should not depend on an API Apple can remove.

**What reversed that.** The gap is real and reachable: a canvas window covered by
another app's overlay cannot be clicked by ANY public method. `accessibility` needs
an addressable element (a canvas has none), and `app_post` is delivered to the app
but ignored by Chromium- and Catalyst-based renderers, which hit-test against the
window server's idea of what is in front. Hit in practice on a Freeform canvas behind
a Zoom annotation overlay: every public method refused or silently did nothing.

**How the trade is contained**, rather than accepted wholesale:

- the symbol declarations and event recipe come from prior permissively-licensed
  open-source reverse-engineering work, attributed in `NOTICE` rather than in code
  comments (no third-party code is copied — the implementation is independent);
- the private ABI lives in ONE module, `macos_skylight.py`, so a macOS point release
  that changes a byte offset has one review boundary. `macos_ffi.py` keeps its
  "public frameworks only" property, which is what makes it reviewable against
  Apple's documentation. `test_computer_use_skylight.py` asserts that quarantine
  structurally — the private symbols must not appear in any other module;
- it **fails closed with a readable refusal.** `available()` reports which symbols
  are missing, and every entry point refuses in prose naming `app_post`. A future
  macOS that drops `SLEventPostToPid` costs the model one clear refusal, never a
  crash and never a mis-delivered click;
- it is reachable **only by name.** `auto` never resolves onto it, exactly as with
  `global` — a model that did not ask for a private-API path never gets one;
- the **byte layout and event order are pinned by tests**, because they are the parts
  an edit can change without a linter or type checker noticing, and the failure mode
  is not an exception: it is a click delivered to the wrong window. The primer
  down/up pair at `(-1, -1)` is asserted present for the same reason — it is what
  routes the real click to the target rather than to whatever is frontmost.

Note the shape other shipped implementations of this technique have converged on: at
least one isolates the private surface in a **separate signed helper process** it can
update or revoke independently of the main binary. Quarantining to a module is the
same instinct one level less strict — worth revisiting if this surface grows, since a
process boundary also bounds a crash, not just a review.

### Audit

Every pointer-moving action emits a SEL record naming the METHOD —
`gate.audit_pointer_move`, `tool_kind="computer_use_pointer"` — on the allow path as
well as the deny path, and *in addition to* the ordinary `tool_invocation`
record. The generic record cannot answer "did the agent ever take control of my
mouse?" because a pointer-moving click is indistinguishable from an `AXPress` in it,
and that is the one question this path exists to keep answerable.

### FFI notes (each cost a debugging cycle)

- `CGEventCreateMouseEvent` takes a `CGPoint` **by value**. Two bare doubles have
  the same total size but a different AArch64 register layout, so the *button*
  argument lands in the wrong register.
- There is **no generic "mouse down"** — the event type is PER-BUTTON. A right-click
  posted as `kCGEventLeftMouseDown` with `button=1` is delivered as a LEFT click.
  `macos_ffi.MOUSE_EVENT_TYPES` carries a distinct down/up/dragged triple per button.
- A double click is **not** two pairs: it is a pair whose `kCGMouseEventClickState`
  is 2, which is where AppKit reads `NSEvent.clickCount` from.
- A drag needs the intermediate `MouseDragged` events (`DRAG_STEPS = 6`) and a small
  per-step delay. A bare down/up pair is not a drag to most apps — the gesture is
  recognized from the motion, and identical timestamps let the recognizer coalesce
  the sequence. TextEdit selected nothing without both.
- Mouse events use the same PRIVATE event source and the same explicit
  `CGEventSetFlags(event, 0)` as keystrokes: the modifier hygiene is about the source
  of the event, not about whether it is a keystroke.
- `CGEventPost` and `CGWarpMouseCursorPosition` are called from exactly two
  functions (`post_mouse_global`, `post_mouse_drag_global`), and a test pins that
  call-site set — a new caller would be a new ungated path to the operator's mouse.

---

## What one accessibility walk reads (and why each field is worth its round-trip)

The tree is the primary channel, so every field it omits is a turn the model spends
recovering the information some other way — usually by guessing a coordinate and
reading back a screenshot. These are the reads that pay for themselves.

**Element frames** (`ElementRec.frame`, from `AXPosition` + `AXSize`). Both come
back as **`AXValue` boxes**, not plain numbers, so each is type-checked with
`AXValueGetType` before `AXValueGetValue` unboxes it into the matching struct — a
`CGPoint` read into a `CGSize` would transpose `y` into `width` and produce a
plausible-looking rect pointing somewhere else, which is strictly worse than no
rect. A half-read (position without size) yields `None` rather than a partial
rectangle for the same reason.

Frames are **window-local**, and `Snapshot.window_bounds` publishes the origin they
are relative to. Three reasons, in order of how badly the alternative fails:

1. the screenshot the model may also be reading is a **crop of the window**, so a
   screen-absolute rect could not be related to any pixel it can see;
2. a window-local frame survives the user dragging the window between turns;
3. an unlabelled coordinate is unusable — a consumer cannot tell window-local
   `(12, 40)` from screen-absolute, and the difference is the window's position.

Because `computer_click(x, y)` takes SCREEN coordinates, the rendered origin line
states the conversion explicitly rather than leaving the model to infer that two
coordinate systems are in play. Without the origin, a frame passed straight to a
coordinate click lands off by the window's position — and on a maximised window it
would appear to work, which is the worst way for this to fail.

**Traits** (`selected`, `expanded`, `editable`). Read as a **tri-state**: only a
definite `True` renders a word, because `AXSelected` is unsupported on the great
majority of elements and collapsing absent into `False` would attach a trait to
every node in the tree. `editable` is the load-bearing one and comes from
`AXUIElementIsAttributeSettable(AXValue)`, not from `AXEnabled` — those answer
different questions. A read-only text field (a disabled form input, a log pane, a
computed cell) reports `AXEnabled=true` with a readable value, so it is
indistinguishable from a writable one in the tree: the model types into it, gets an
`ok` result, and the text silently goes nowhere. Settability is the only signal that
separates them, and it turns that dead end into something visible *before* acting.

**Focus and selection** (`ElementRec.focused`, `Snapshot.selected_text`). Both are
read ONCE per walk off the **application** element, not per node and not
system-wide. Per-node would add a round-trip to each of up to ~1,400 nodes to answer
a question with one answer. System-wide focus follows whatever the *operator* is
working in, so it would mark a background app's element whenever the target happened
to be frontmost and report nothing the rest of the time; the app-scoped attribute
answers "where is this app's caret", including for a background app. Identity is
compared with `CFEqual`, never pointer equality: `AXFocusedUIElement` is a
Copy-Rule read returning a fresh reference to a node the walk reaches under a
different one, so an address comparison would answer "not focused" for every element
and the marker would silently never appear.

**Alternate child collections** (`AXRows`, `AXVisibleChildren`, merged with
`AXChildren`). `AXChildren` alone is not the whole tree: a table, outline or list
routinely exposes its rows ONLY through `AXRows`, and a scrolled list only the
on-screen ones through `AXVisibleChildren`, reporting an empty or scaffolding-only
`AXChildren`. A children-only walk therefore rendered a spreadsheet, a Finder list or
a mail inbox as a container with nothing in it — which reads as "this app has no
content" rather than "you looked through the wrong attribute". For a row-bearing role
the alternates are read FIRST so the rows take the low indices a model actually
addresses. Merged results are deduplicated by element **identity** (`CFEqual`), since
a row is commonly in both collections and each read mints a distinct reference: a
duplicated row is worse than a missing one, because the model would address two
indices believing they are two different rows. `MAX_CHILDREN_PER_NODE` bounds the
**merged** list, so three collections cannot together exceed what one was allowed to.

**Secure elements disclose only their existence.** No title, no value, no traits and
no frame. `editable` would confirm the password box accepts input and a rect would
locate it precisely enough for a coordinate click, so both are withheld for the same
reason the value is. The selection read is gated on the **focused** element
specifically rather than the window's `has_secure`, because a window may hold a
password field while the caret sits in an ordinary search box — refusing there would
withhold something that is not sensitive.

### The click ladder's last rung: the enclosing control

`AXPress` → `AXConfirm` → `AXOpen` recovers most element clicks. What it does not
recover is web content, which renders a clickable row as a plain `AXStaticText`
inside a pressable wrapper: the text node advertises no actions and refuses every
verb, so an element click reported failure for a row a human clicks without
thinking — leaving coordinates as the only move, which is what the element path
exists to avoid.

So a failed ladder tries the nearest ancestor that plausibly IS the control. An
unguarded version of this is a wrong-click generator (climb far enough and something
is always pressable — eventually the page), so it is bounded twice:

- **hops** (`MAX_ANCESTOR_PRESS_HOPS = 3`) — beyond that the ancestor is more likely
  the page than the row;
- **area** (`MAX_ANCESTOR_AREA_RATIO = 8.0`) — the real signal that an ancestor is
  "the same control" is that it is roughly the same SIZE. A row wrapping a text node
  is a small multiple of its area; a scroll area or page body is orders of magnitude
  larger. **Without the target's own frame there is nothing to compare against, so
  the fallback declines rather than guessing** — and declines before spending any AX
  round-trips on a climb it could not judge.

Only `AXPress` is attempted (not the full ladder): `AXOpen` on a container can mean
something quite different from activating the row inside it. It never applies to a
right click, for the same reason the menu ladder never falls back to a press — a
context menu on the wrapper is a different menu. And the result **says** it pressed
the enclosing control, because the model has to be able to tell "my click worked"
from "something near my click worked".

---

## Index lifecycle (and its honest limit)

Element indices address a LIVE user interface, so the snapshot cache
(`index.py::SnapshotIndex`) is a correctness control, not an optimization.

**Entries are keyed by `(session_key, window_key)`** — and both halves of that key
came from a review finding.

*The window half.* Element indices address one WINDOW's accessibility tree, so
keying by application alone aliased distinct windows of the same app: snapshot
document A, focus document B, and the follow-up action — which re-resolves to B —
retrieved A's cached tree. The fingerprint check cannot catch that, because two
documents of the same app routinely have identically-shaped toolbars, so
`role|subrole|title` at a given index matches and the action mutates the wrong
document. `AppRef.window_key` is the app identity plus **pid and window id** (a
window id is only unique within a session, and a relaunched app can reuse one).
`AppRef.key` deliberately stays window-agnostic — it is the denylist and
allow/deny-pattern identity, where "block Terminal" must mean every Terminal window.

*The session half.* The gateway is
one process serving every surface — dashboard tabs, Slack threads, cron jobs — so an
app-only key made the cache shared mutable state across concurrent sessions: session
A walks Preview and is shown indices, session B then walks Preview after the UI
moved and its snapshot REPLACES A's, and A's next action resolves *and*
fingerprint-verifies against B's tree. Both sessions look internally consistent and
the wrong control is activated. Lifecycle is per-session too: `computer_end_turn`
drops only the calling session's entries (a process-wide clear would let any surface
turn another's next action into a spurious "call `computer_get_state` first"), and
`MAX_INDEXED_APPS` is a per-session cap so a chatty surface cannot evict another's
live indices. `SnapshotIndex.clear()` remains the process-wide reset, reachable only
from lifecycle callers (a backend swap), never from a tool. Namespacing removes the
CROSS-session race entirely; it does not remove the within-session one below, which
is inherent to driving a live UI.

Three mechanisms bound a single session's validity, none of which needs a
turn-boundary signal kiro-cli does not have:

1. **Hard fail, never lazy re-snapshot.** Acting on an app with no cached
   snapshot is refused: `Error: no state for 'Finder'. Call computer_get_state
   first.` A lazy re-walk would let the model act on a tree it was never shown,
   which is exactly the failure indices exist to prevent.
2. **TTL** — `SNAPSHOT_TTL_SECS = 90`, `time.monotonic()` throughout so a clock
   adjustment cannot make a stale snapshot look fresh. Expired:
   `Error: state for 'Finder' is 214s old. Call computer_get_state again.`
   `MAX_INDEXED_APPS = 8` bounds RSS (each snapshot holds its encoded JPEG).
3. **Fingerprint drift** — the real guard, run unconditionally before every
   mutating action against a FRESH walk (measured 40-70ms: Finder 145 nodes /
   0.04s, Chrome 1431 / 0.07s). On drift: `Error: element_index 7 changed since
   the last computer_get_state (was 'AXButton "Save"', now 'AXButton "Delete"').
   Call computer_get_state again.` The fresh walk becomes the new cached
   snapshot. The fingerprint is `role|subrole|title` — `value` is deliberately
   excluded, because a text field's value changes as the user types without the
   control's identity changing, and folding it in would refuse almost every
   legitimate action. A secure record contributes only role/subrole/title, so
   fingerprinting never reads credential bytes.

**Both of a mutating action's walks re-use the budget the CACHED snapshot was
walked at**, carried on `Snapshot.walk_budget` (stamped by `service.snapshot`, read
back by `tools._mutation_walk_budget`). A mutating tool takes no `max_tree_nodes` /
`max_tree_depth` / `text_limit` arguments — the model set those on the
`computer_get_state` that produced the indices — so building either walk from the
`config.json` default silently shrinks the tree: an element the model was
legitimately shown at `max_tree_nodes=2001` resolves to *"no element at that index"*
in the drift check, and the post-action refresh it is handed next is truncated
identically, so calling `computer_get_state` again reproduces the same refusal
forever. That is a documented happy path (the MCP schema advertises up to 5000 and
the Settings copy says to raise it for dense apps), which is what made the loop
worth a stamped field rather than a comment. `want_image` is still forced off for
both — the verification compares structure, and spooling a screenshot nobody asked
for would double the cost of every action. Pinned by
`test_mcp_computer.py::TestTheDriftWalkHonoursTheSnapshotBudget`, which asserts the
budget off the driver's own call journal (a behavioural assertion passed while the
refresh walk was still using the default).

Plus `computer_end_turn` for explicit early release, and `reset_shared_backend()`
drops the cache too (indices from one driver's walk are meaningless against
another's).

**Honest limit — fingerprinting narrows the race, it does not eliminate it.** A
tree can still change between the verifying walk and the action microseconds
later. It converts a silent wrong-click into a loud refusal in the overwhelming
majority of cases; it is **not** a transactional guarantee, and the spec says so
rather than overclaiming. Every mutator re-snapshots, and every mutator is
interactively approved by default so a human sees the prompt.

Indices are also *dense*: elidable containers (`AXGroup`, `AXUnknown`,
`AXSplitGroup`) with no title or value are dropped WITHOUT consuming an index,
and `index.resolve()` looks an element up by its own `index` field rather than by
list position — position and index are not interchangeable.

---

## The ctypes layer: four findings from running real code

All native work is confined to `macos_ffi.py`. `import ctypes` is a top-level
import statement (AUTOSDE `top-level-imports`), but **no `CDLL`/`find_library`
runs at module scope** — the four frameworks (CoreFoundation,
ApplicationServices, CoreGraphics, ImageIO) load inside `_frameworks()`, cached
in a module global, raising `ComputerUseUnsupported` off macOS. A module-level
`CDLL` would raise `OSError` on the Linux CI fleet at import time and break
collection of every test that transitively imports the package.

**1. A missing `argtypes` is a SIGSEGV, not a TypeError.** ctypes marshals a
Python int as a 32-bit C int and TRUNCATES the 64-bit pointer. This produced a
real `EXIT=139` on the first prototype run; adding explicit
`CFGetTypeID.argtypes` / `CFStringGetLength.argtypes` / `CFRelease.argtypes`
fixed it. Therefore: one declarative `_FN_SPECS` table, one bind pass at first
`_frameworks()` call so no function can be reached un-bound, and `_bind` RAISES
when `argtypes is None`. `CGPoint`/`CGSize`/`CGRect` are real `ctypes.Structure`s
— passing two doubles where a struct is expected mis-marshals the call. A test
asserts every row has both a non-None `argtypes` and a `restype`, and that
`CGEventPost(` appears nowhere in the module while `CGEventPostToPid` does.

Related hygiene: `_cf_string()` is a context manager that `CFRelease`s on exit (a
tree walk creates thousands of CFStrings; leaking them is a real RSS bug the
watchdog would eventually recycle the session over), and `ax_attr()`/`ax_str()`
type-check `CFGetTypeID(v)` against `CFStringGetTypeID()`/`CFArrayGetTypeID()`
**before** using a value — a wrong-type read is another segfault, not an
exception.

**2. Electron/Chromium apps need an explicit opt-in.** Chrome returned
`kAXErrorCannotComplete = -25204` for every attribute read. Setting
`AXManualAccessibility = kCFBooleanTrue` on the app element and waiting ~2s
unlocked **1431 nodes in 0.07s**. Without this, Slack, VS Code, Obsidian and
KiroCrew's own desktop app appear permanently empty. Order of operations:
create the app element, immediately
`AXUIElementSetMessagingTimeout(app_elem, AX_MESSAGING_TIMEOUT_SECS)` —
mandatory, because ctypes releases the GIL around the C call and a genuinely hung
target app would otherwise park the worker thread indefinitely — read
`AXWindows`, and on `-25204` set the opt-in, poll for up to
`ELECTRON_OPT_IN_WAIT_SECS` in 0.25s steps, retry **once**, then raise naming the
raw AX code so a support thread is diagnosable. The first `computer_get_state`
on an Electron app is inherently slow; the skill says so, so a model does not
read it as a hang.

**3. `pgrep` is the wrong way to resolve an app to a pid.**
`pgrep -n "Google Chrome"` returned 47492 — a short-lived helper that vanished
seconds later and answered `-25204` to everything — while the real browser was
637. `pgrep -n Slack` gave 1614 (helper) against the window list's 942 (real).
Resolution is therefore ALWAYS from `CGWindowListCopyWindowInfo` with
`kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements`, keeping
`kCGWindowLayer == 0` entries and taking `kCGWindowOwnerPID`. `list_apps()` is
built from the same window list and nothing else. A test asserts the source text
contains no `pgrep`.

**4. Synthesized key events inherit the user's LIVE modifier state.** Typing
`"a","b","c"` into TextEdit produced **`' I Abc'`**. Three rules, all
load-bearing and all verified to then produce exactly `'abc'`:

```python
src = cg.CGEventSourceCreate(kCGEventSourceStatePrivate)  # 1: PRIVATE, never None
ev  = cg.CGEventCreateKeyboardEvent(src, keycode, is_down)
cg.CGEventSetFlags(ev, flags)   # 2: ALWAYS called, even when flags == 0
cg.CGEventPostToPid(pid, ev)    # 3: app-targeted, never the global tap
```

`flags` is built from ZERO and OR-ed with only the caller's parsed modifiers.
`CGEventPost` (global tap, hits whatever is frontmost — i.e. whatever the user is
actually doing) appears nowhere.

Two smaller findings encoded the same way: **the advertised action list lies** —
an `AXScrollArea` advertising `AXScrollDownByPage` returned
`-25205 (kAXErrorActionUnsupported)`, so `scroll` attempts the AX action, checks
the error code, and falls back to `CGEventCreateScrollWheelEvent` posted with
`CGEventPostToPid`; and **the tree walk is ITERATIVE** (an explicit stack), so a
pathological deep tree cannot `RecursionError` inside ctypes.

**Secure fields are a subrole, not a role.** A real macOS password box reports:

```
AXRole    = 'AXTextField'        <- looks like an ordinary text field
AXSubrole = 'AXSecureTextField'  <- the ONLY reliable signal
AXValue   = readable
```

Checking `AXRole == "AXSecureTextField"` — the intuitive check — **misses every
password field**. The driver sets `secure = (role == SECURE_SUBROLE or subrole ==
SECURE_SUBROLE)`, and three protections key off that one flag: `render` emits
`<secure>` for the value (and for the title, which is sometimes the account
name), `policy.check_input_target` refuses `set_value`/`type_text`/`press_key` at
a secure target, and a window containing ANY secure node gets **no screenshot at
all**. Whole-window suppression rather than a blanked rectangle: there is no
reliable way to blank a sub-rectangle of an already-encoded JPEG, and a partial
redaction that missed would be worse than none.

**Every walk cutoff sets a flag, and the capture gate treats "unknown" as
"present".** `MAX_CHILDREN_PER_NODE = 512` bounds one pathological container (a
table with 100k rows) before the global node budget notices — but it used to drop
the tail *silently*, so `saw_secure` reflected only the first 512 children while
`truncated` stayed False. The walk then reported itself complete and non-secure and
`capture_snapshot_image` had nothing to refuse on, leaving a password field as child
513 with the whole window's pixels capturable (reviewer finding). A capped read now
sets `truncated`, which already means "there is more of this tree than you were
shown". Detected as `len(children) >= limit` rather than by re-reading the array for
a real count (that would reintroduce the cost the cap exists to avoid), so a node
with exactly the cap many children is treated as truncated too — a false positive
costs one suppressed screenshot, a false negative is the disclosure.

**And that suppression ANNOUNCES itself** (`TRUNCATED_WINDOW_NOTE`, emitted by
`render._render_image_note`). It did not: the renderer special-cased only
`has_secure` and returned `""` for every other empty `image_path`, so a truncated
walk produced no image and no reason. Truncation is the NORMAL state for a
Chromium/Electron window at the shipped 1200-node default (Chrome measured 1475
nodes), so `screenshot: true` on Chrome, Slack or VS Code silently returned nothing
— the retry loop `SECURE_WINDOW_NOTE` and `OBS_SUPPRESSED_NOTE` exist to prevent.
The note names the remedy (raise `max_tree_nodes` / `max_tree_depth`), because
unlike the secure case this one is fixable by the caller, and it is checked AFTER
`has_secure` so a window that is both keeps the more specific reason. The shipped
`FakeComputerUseBackend` mirrors the refusal too — it previously attached pixels to
a truncated walk, so deleting the production branch left the suite green.

**It fires only when an image was actually REQUESTED** (`walk_budget.want_image`;
`None` — an unstamped, backend-built snapshot — still announces, since there the
request is unknown and a spurious note is cheaper than a silent omission). Without
that condition the fix inverts into the same defect: every mutating action's refresh
walk forces `want_image=False` by design, so a successful click on a browser window
came back announcing the suppression of an image nobody asked for and telling the
model to "re-run with a higher `max_tree_nodes`" — an argument mutating tools do not
accept (reviewer finding on the first version of this fix). Both directions are
pinned by `test_computer_use_snapshot.py::TestASuppressedScreenshotAlwaysSaysSoWhy`,
including one case through the real `dispatch_tool` path, because neither is visible
to a `render_tree` unit test on its own.

**Every refusal is audited, including the pre-gate ones.** The gate audits its own
denials and `_audit_allowed` records permitted calls, which left a hole between
them: a schema `ValidationError`, an unknown tool, a bad `click_method`, a stale
index, an unparseable key and the paste refusal all return through `_refusal`
*without* reaching the gate, so nothing was recorded (reviewer finding). An audit
trail with a gap at "malformed or refused attempts" is the wrong shape for this
surface — a burst of them is exactly the signal an investigation wants. There are two audited exits and no third: `_refusal`
for text that can quote the desktop (it also traverses the observation ceiling and
the redaction pass), and `_static_refusal` for this package's own static prose about
the caller's request — an unregistered tool, the feature being disabled, a missing
`element_index`, a coordinate form under a targets ceiling, a malformed pointer
request, a governance denial. The second helper exists because six of those sites
returned `f"{ERROR_PREFIX}{…}"` inline and so were still unaudited after the first
fix; an AST test now asserts no other function in the module builds that string, so
a seventh cannot be added unaudited. Both emit a `refused` `log_tool_invocation`
with the tool name and **no resources**: the refusal text can quote a window title and this event fires before
the observation ceiling has been applied to it, so the audit line carries the fact,
never the desktop detail ("redacted credentials" is a weaker guarantee than "never
included").

**Paste is refused outright.** `computer_press_key` rejects any Command+V or
Control+V chord (`keymap.is_paste_shortcut`, keyed on the RESOLVED keycode+flags so
`command+V` / `super+v` / `meta+v` / `cmd+shift+v` cannot spell around it). The
clipboard is out of band: KiroCrew never reads it, so nothing can classify what it
holds, and a paste into an ordinary readable field puts that content into the tree
the very next snapshot returns. The secure-target refusal cannot help here — the
*destination* is not a secure field, and the credential arrives from outside every
channel that gets inspected — so the disclosure is "whatever the operator last
copied", which is routinely a password from a password manager's copy button.
Typing known text stays available and is the pointer the refusal gives: the content
of `computer_type_text` is inspectable, so the sensitive-text scan can actually run
on it.

**Keyboard input refuses when the target will not take focus.** Both keyboard
tools require `element_index` precisely so the addressed element can be inspected
for that flag — and the macOS driver sets `AXFocused` on it before typing so the
keystrokes land there. That focus attempt used to be best-effort: on failure it
logged at debug and typed anyway, which delivered the input to whatever the app
focused LAST — an element no check ever saw, and possibly the password box the
refusal above exists to protect (reviewer finding). `type_text` and `press_key` now
return a failed `DriverResult` naming what did *not* happen ("the keystrokes were
NOT sent"), because a model told only "focus failed" would assume the input landed
and go verify a change that never occurred.

### Threading

The gateway is async and the native work is blocking, so every accessibility
walk and capture is offloaded:
`await loop.run_in_executor(subprocess_executor(), fn, ...)`.

`subprocess_executor()` and never the DEFAULT pool — for both `tools.dispatch` and
the gateway handler's `_dispatch_off_loop`. That pool is the one the repo reserves
for calls that can block on a wedged external resource, and a hung target app
parking a worker for the whole `AX_MESSAGING_TIMEOUT_SECS` is exactly that: on the
default pool a few wedged desktop calls would starve every other
`run_in_executor(None, …)` user in the gateway and the loop's own `getaddrinfo`.
(The handler's *config* reads/writes do use the default pool — short, bounded
filesystem work.) Verified safe:
4 concurrent AX walks in threads returned identical node counts (146 — no CF
thread-safety corruption) while an asyncio heartbeat logged **40/40** ticks, so
the loop never stalled. The AX messaging timeout above is what bounds the
worst case of a hung target app parking a worker.

---

## Screenshot pipeline (measured, not guessed)

Capture and encode are **100% in-process ctypes** —
`CGWindowListCreateImage` → ImageIO `CGImageDestinationCreateWithData` /
`AddImage` / `Finalize` with exactly two option keys
(`kCGImageDestinationImageMaxPixelSize`, `kCGImageDestinationLossyCompressionQuality`).
No subprocess on the observation or input path, and therefore no Pillow
dependency (Pillow is declared in neither `setup.cfg` nor `pyproject.toml`). The
package has exactly three spawn sites, each with a `test_spawn_audit.py::BENIGN_SPAWNS`
entry and each structurally bounded: `overlay.py` launches the cursor renderer
(`sys.executable -m <module>`, no agent-supplied argument), and the two
`launch_*.py::spawn_detached` sites are `computer_launch_app`'s whole purpose —
opening an application IS creating a process. None of the three carries an
agent-supplied argv; `test_computer_use_unsupported.py` pins that by source. All of
`img`/`data`/`dest`/`opts` and every CFNumber/CFString are released in a
`finally`, including when `Finalize` returns `False` (which degrades to a
tree-only result rather than raising).

Compression defaults are computer use's **own** 1280px / q55 — deliberately NOT
browse's 1920/q70 — because the accessibility tree is the primary channel and
the image is corroboration. Measured on a real 1840x872 window:

| Width | Quality | Bytes | ~Tokens | Note |
|---:|---:|---:|---:|---|
| 1840 (orig PNG) | — | 123,343 | 41,115 | unusable |
| 1920 | 70 | 48,889 | 16,297 | browse's defaults — only 60% off |
| **1280** | **55** | **24,766** | **8,256** | **shipped default**, visually verified fully legible |
| 1024 | 55 | 17,918 | 5,973 | still readable, tighter |
| 800 | 40 | 10,781 | 3,594 | small text starts to soften |

The 1280/q55 output was decoded and inspected: every sidebar label, filename,
toolbar icon and the selected-item highlight are clearly readable. For contrast,
the reference runtime attached a native-resolution PNG unconditionally and no
parameter capped it — a Slack window measured **~437KB base64 ≈ 109K tokens** on
one call.

Files land in `os.path.join(tempfile.gettempdir(), "kirocrew-computer-shots")`
(the idiom its test pins by source text), created `mode=0o700`, each file passed
through `platform_compat.restrict_to_owner`, ring-trimmed to
`SCREENSHOT_KEEP = 200`. Only the **path** is ever relayed; the bytes never enter
a result.

---

## The live view (PiP)

`computer_use/screencast.py` + `website/src/components/ComputerUseLiveView.tsx`.
The machine being driven is often not the machine the operator is looking at (a
cloud Mac, a session reached over the reverse SSH tunnel, or another Space), so a
floating picture-in-picture panel mirrors what the agent sees. It rides an
existing capture rather than opening a new one: `emit_snapshot_frame` POSTs the
already-encoded JPEG to the loopback `/api/computer-use/frame` ingress, which
rebroadcasts it to OWNER websockets as a `computer_use_frame` event. The
in-gateway browse mirror this shape was modelled on is gone — browsing now runs
through the external `playwright-cli` binary (`kiro_crew/browser_cli/`), which
serves its own dashboard (`playwright-cli show`, supervised by
`browser_cli/view.py`) instead of relaying frames through the gateway.

**It is a RELAY, not a capture.** `capture_snapshot_image` hands the JPEG it just
encoded for the model to `emit_snapshot_frame`. There is no timer, no second
`CGWindowListCreateImage`, no full-screen grab, and no way for the panel to ask
for a frame — so opening it cannot make the agent screenshot anything, and it can
never show a window the model did not already read. Frames are therefore sparse
(one per `computer_get_state` with `attach_screenshot`) and are always the
already-downscaled 1280px/q55 bytes.

**Three suppressions, all evaluated before anything leaves the process** (the
ingress handler is NOT the boundary and is not relied on as one):

1. **No published surface scope → no frame.** The capture layer has no session
   identity of its own (`SnapshotRequest` carries budgets), so
   `api_computer_use_invoke` wraps its one blocking dispatch in
   `screencast.frame_scope(...)`, publishing `(session_key, agent, app)` on the
   worker thread. It is `threading.local` — not a contextvar, which would not
   survive the executor hop, and not a module global, which would leak one
   surface's identity into the next dispatch on the same pooled thread — and it
   restores the previous value on exit. A capture reached any other way (a CLI
   probe, a future caller that skipped the handler) emits nothing rather than
   guessing an identity, matching the gate's fail-closed treatment of an empty key.
2. **A secure window is never mirrored**, read from `Snapshot.has_secure` — the
   driver's own predicate, the one `capture_snapshot_image` already refuses on — so
   there is exactly one definition of "this window holds a password field".
3. **A withheld `screenshot` channel emits nothing**, via
   `gate.permitted_observation_channels`: the same evaluator the tool path and the
   Settings snapshot use. That evaluator permits every channel today, so this is
   the seam that would hold if an observation ceiling were reintroduced, not a live
   restriction.

**Wire.** `POST /api/computer-use/frame` (loopback + `X-Internal-Secret`, strict,
both re-asserted in the handler) carries
`{data, format:"jpeg", width, height, session_key, app}`.
`build_frame_payload` bounds every field: base64 charset (which structurally
excludes `:`, whitespace and `<`/`>`, so no URL and no markup) plus a
`MAX_FRAME_B64_CHARS` cap, `format` restricted to the single value `"jpeg"` (a
frame claiming PNG or WebP is **refused, not relabelled** — that is what makes
"never a full-resolution PNG" structural), the dimensions to positive ints within
`MAX_SCREENSHOT_MAX_PX` with `bool` excluded, and the two text fields to explicit
charsets. `app` is the resolved application's display NAME, never the window title
(titles are their own observation channel and can carry document names and paths).

The rebroadcast is `deliver_ws_owners`, **not** `broadcast_ws`: an App Kit
credential can open `/api/ws` and lands in the all-clients set, and a live view of
the operator's desktop must not cross that boundary. One SEL line per frame
records only the delivery count — never the pixels or the mirrored app, so the
audit log does not itself become a record of what was on screen.

The POST runs on a daemon thread (the capture runs in a worker thread, and
`broadcast`/`deliver` are event-loop objects — `ensure_future` off-loop raises) and
swallows every failure; `capture_snapshot_image` additionally wraps the call so
its own "never raises" contract cannot be broken by a decorative mirror.

**Panel.** `hidden → (first frame) open ⇄ chip`, docked bottom-LEFT so it never
stacks with the browse mirror's bottom-right. Draggable, eight resize grips, two
size presets, size persisted in `localStorage`, geometry always fitted back into
the viewport. Close remembers the dismissed session so its later frames do not
re-open the panel; a different driving session still surfaces. Read-only — no
click-through, no input relay, no control channel. The empty state states both
reasons a frame may be absent, so "nothing here" never reads as a fault.

---

## Cursor Motion: a real desktop overlay, cosmetic only

`computer_use/cursor_motion.py` (geometry) + `overlay.py` (gateway supervisor) +
`overlay_proc.py` (the AppKit child). A **fake cursor drawn on the operator's real
desktop**, animated along the path a click is about to take. It exists for one
reason: an unattended desktop action is otherwise unreviewable in real time — a
human glancing at the screen sees a window change with no indication of what
caused it. The overlay makes "the agent is about to press *that*" visible.

It is **purely cosmetic and never on the success path of a tool.** Nothing it does
can make a call fail, and nothing it draws can make a call succeed. Three
structural properties enforce that:

- **It is not the pointer.** The overlay is a drawn image; the physical cursor is
  untouched. Cursor Motion and `click_method: "global"` are independent and
  unrelated — the overlay can decorate an `app_post` click that moves nothing, and
  the pointer path works with the overlay off. Turning Cursor Motion on grants no
  new capability, which is why it is an ordinary `config.json` preference
  (the typed `ComputerUseConfig.cursor_motion` field, **default OFF**) rather than
  a keystone flag.

**Where it is invoked.** `overlay.show_pointer_motion(x, y, count)` is the sync
seam the blocking dispatcher calls from `tools._perform`, immediately before a
gesture whose `moves_pointer` is true — i.e. only `click_method: "global"` clicks
and drags. The app-scoped and accessibility paths never move the physical cursor,
so animating one there would show a gesture that is not happening. The dispatcher
runs on a worker thread, so the seam schedules the animation onto the gateway loop
with `run_coroutine_threadsafe` (bound per request by the invoke handler, which
runs *on* that loop) and does **not** await it: waiting would add the glide's
duration to every pointer click. Ordering is therefore best-effort — the click may
land a few milliseconds before the drawn cursor finishes arriving — which is the
deliberate trade against putting an animation on the critical path.

The Settings row appears only on macOS, and only once computer use itself is
enabled — a glide with no pointer click to accompany it would advertise something
that never happens.
- **It never raises and never blocks the loop.** Every `CursorOverlay` method
  swallows every exception (a dead child, an unavailable AppKit, a full pipe all
  degrade to "no visual cursor") and the animation is fire-and-forget: the command
  is written and the coroutine returns rather than awaiting ~1.4s of decoration on
  the latency path of a real action. Spawn is `asyncio.create_subprocess_exec` with
  one bounded `wait_for` on the readiness line.
- **It is screenshot-invisible BY DESIGN.** The child sets
  `NSWindowSharingNone` (`setSharingType: 0`), verified A/B: the overlay renders on
  the desktop and does not appear in `screencapture` output. That is a correctness
  requirement, not a nicety — the agent screenshots the same screen it is drawing
  on, and a fake cursor in the pixels the model reads would be an artifact the model
  reasons about as if it were part of the application.

### Why a separate process

AppKit needs a main-thread run loop and the gateway's main thread **is** the
asyncio loop, so the overlay cannot live in the gateway at all. `overlay_proc.py`
is therefore its own process (`python -m kiro_crew.computer_use.overlay_proc`,
fixed argv, nothing agent-supplied in it) driven by newline-delimited JSON on
stdin. It has its **own ctypes surface**, which is the one documented exception to
"`macos_ffi.py` is the only module that touches ctypes": that invariant exists so a
native fault cannot take down the gateway, and a fault in a separate short-lived
child costs one animation. The child's docstring says so.

`overlay.py` serializes on one `asyncio.Lock` — the child's stdin is an ordered
byte stream and two concurrent writers would interleave half-lines — and the lock
is created lazily, because an `asyncio.Lock` built outside a running loop is the
cross-loop hazard `kiro_crew/__init__.py` documents.

Off macOS, and whenever the opt-in is off, `cursor_motion_enabled()` returns False
before anything else in every method, so a Linux CI shard exercises these bodies
and observes that no child is spawned. `show_pointer_motion` additionally no-ops
when no gateway loop is bound — a coroutine scheduled onto a loop nobody runs would
never execute, and a closed loop (a gateway restart) is treated the same way. The
config read stays `getattr`-based even though the field is now declared, and fails
to OFF: an unreadable setting can only ever mean "no decoration", never "start
drawing on the user's screen".

### The path model is pure geometry

`cursor_motion.py` has **no ctypes, no subprocess, no AppKit, no config read** —
given a start and an end point it returns sampled screen points plus a duration,
and every function is total in its arguments. That is what makes the *feel* of the
animation unit-testable on a display-less CI shard: a regression in how the cursor
moves fails an assertion on numbers rather than requiring somebody to watch a
screen. The shape is one cubic Bezier bowed by a perpendicular arc
(`clamp(distance * 0.22, 28, 110) * curve_scale`), asymmetric control points so the
cursor leaves fast and arrives settling, driven by a velocity-Verlet spring on
**progress** (not position) at a fixed 1/240s step — so the spring's slight
numerical overshoot past 1.0 is clamped before sampling and the drawn cursor can
never overshoot the point it is advertising.

Coordinates are **top-left** everywhere in this module, matching the AX/CG
surfaces and `screencapture -R`. The bottom-left flip AppKit's `NSWindow` origin
needs (`y_bottom = H - y - h`) happens in `overlay_proc` alone, at the one place
that actually talks to AppKit.

---

## Always-on floors (no policy key, and none will be added)

These are the protections that survive the governance removal, and they survive
BECAUSE they were never governance keys. Each is unconditional code — no policy
file, no profile, no toggle — which is exactly why removing the ceiling did not
touch them. They sit with `_SENSITIVE_HOME_DIRS` and the AKIA redaction:

- **Secure-field value redaction** — `render` emits `<secure>`; the value bytes
  are never rendered, not truncated, not masked-with-a-hint.
- **Whole-window screenshot suppression** when any node is secure.
- **Credential / exfil redaction** — every renderer ENDS with
  `policy.redact_result`, which routes through `platform.redact_via_context` (so
  a loaded companion's extra credential and cookie patterns apply). This is the
  primary egress control for tree text, not belt-and-suspenders: live probes
  observed real filesystem paths, mounted volume names, bundle ids and document
  names in accessibility trees and window titles.
- **Refusals go through the SAME exit as results** (`tools._refusal`). A refusal is
  prose about the desktop: a fingerprint-drift message embeds
  `render.describe_record` for the cached AND the fresh element (two verbatim
  accessibility titles), a driver failure quotes the app label, `check_app` names
  the resolved bundle id. So `_refusal` applies both controls in the same order the
  result path does — `gate.apply_observation_ceiling` over the sentence as the
  `text` channel, then `policy.redact_result` — and when `element_values` is denied
  it replaces every quoted fragment with `<redacted:policy>`, keeping only the
  actionable "call `computer_get_state` again" half. Without this, provoking a drift
  would be the one path around both the redaction pass and the observation ceiling.
  Refusals that are 100% KiroCrew's own static prose (the primary-enable refusal, the
  generic governance denials, the "pass an `element_index`" hint) skip it by
  construction: no desktop text to leak, and redaction could only mangle them.
- **Per-call SEL audit** — every permitted call emits `log_tool_invocation`, every
  denial `log_governance_decision`. The redaction inside those writers is
  load-bearing: a denied `set_value` reason can carry the text the agent tried to
  type, and an `item` can be a path-bearing window title.

**Refuse any future request for a policy key that re-enables secure-field
values.**

---

## Platform support

| Platform | State |
|---|---|
| macOS | Supported. ApplicationServices AX + CoreGraphics + ImageIO via ctypes. |
| Windows | **Full tool set.** UIAutomationCore over raw ctypes COM (no `comtypes`) + `PrintWindow`/GDI+. Element-addressed actions go through UIA control patterns and move neither the pointer nor focus; the pointer and keyboard routes exist but are opt-in by name, because Windows has no per-process input delivery. See [The Windows driver](#the-windows-driver). |
| Linux | `LinuxBackend(UnsupportedBackend)` — typed refusal. The plan is AT-SPI over D-Bus; Wayland has no unprivileged window capture (it needs xdg-desktop-portal), so a first cut may be tree-only, which `SnapshotRequest.want_image` already accommodates. |

Every unimplemented path **refuses rather than raises**, mirroring how
`dashboard/handlers/terminal.py` handles the Windows PTY, and names what is
missing so a user learns something and a maintainer finds the next step. When
`supported` is false the Settings panel renders the reason and no toggle.

### The Windows driver

Reading and input both work through real UI Automation, and the whole layer above
the driver (the keystone, schema validation, the snapshot index, fingerprint
drift, redaction, the SEL audit) runs unchanged. Files: `windows_ffi.py` (the only module touching Windows-native code),
`apps_windows.py`, `snapshot_windows.py`, `capture_windows.py`, `windows_driver.py`.

Five things differ from macOS in ways that are load-bearing rather than
incidental. Each was measured on Windows 11:

- **A host process's name is not an app identity either, so a hosted window is
  named by its TITLE.** `ApplicationFrameHost.exe` fronts every packaged/UWP app, and
  `policy.check_app` matches `bundle_id` and `name` — so taking the host's name gave
  an operator two bad options and no good one: a deny rule on the real app matched
  NOTHING, and the only pattern that did match (the host) blocked every packaged app
  at once. Measured with *Settings* and *HP Audio Control* both reporting the host.
  Both policy-matched fields therefore carry the window title, which is the only
  identity such a window has that an operator would write down. Its instability
  across documents is the accepted cost; the alternative is an action in an app they
  excluded.
- **Re-addressing an element compares role AND name.** A toolbar that swapped Save
  for Delete leaves both as `Button` at the same index, so a role-only check accepts
  Delete and the action presses it — the model's "click Save" deletes the user's
  work. The name is already computed for the elision test, so this costs nothing.
- **A pid is not an app identity, so confinement keys on the HWND.**
  `apps_macos.pid_owns_point` is sound because macOS delivers input per-process;
  Windows delivers none per-process, and one broker fronts many apps —
  `ApplicationFrameHost.exe` was observed hosting *Settings* and *HP Audio
  Control* simultaneously under a single pid. `apps_windows.hwnd_owns_point`
  therefore requires `GetAncestor(WindowFromPoint(p), GA_ROOT)` to equal the
  app's own top-level handle, and `list_apps` emits one entry per WINDOW so two
  frame-host siblings cannot share a grant. Every pointer gesture calls it — the
  `global` click and EVERY point of a drag's path — before any event is sent. Every
  point rather than the two endpoints, because a `curved` path bows away from the chord
  (see § Multi-point dragging) and a stroke between two authorized points near an edge
  can otherwise travel over the window beside it with the button held.

  `window_list()` raises rather than returning a short list. CPython cannot raise out
  of a ctypes callback — it prints "Exception ignored" to stderr and returns 0 — so an
  unguarded failure inside the `EnumWindows` callback dropped that window and every
  one after it while `EnumWindows` still reported success: raising in every second
  callback yielded 135 of 270 windows with nothing reaching Python. The callback
  records failures and the caller re-raises, because "no applications on screen" must
  never be the answer to a read that could not be performed.
- **The bounded walk is mandatory, not an optimization.**
  `FindAllBuildCache(Subtree)` ignores a node budget entirely: it built 2966
  nodes in **13.7s** on a real Chromium window, where `windows_ffi.walk_bounded`
  reached the shipped 1200-node budget in **630ms**. A mutating action walks
  twice, so that is ~27s against ~1.3s per click. Cached property reads are what
  make even that viable — 176 reads in 1.4ms against roughly 0.6–1.5ms *each*
  live, because every live read is a cross-process RPC. Per-node cost ranges from
  ~2ms (UWP) to ~20ms (WPF).
- **The secure flag is read LIVE and decided fail-closed.**
  `UIA_IsPasswordPropertyId` is **30019**; 30097 is `LegacyIAccessibleHelp` and
  returns a `VT_BSTR` that is empty — hence falsy — for a real password box with
  no placeholder, so it fails OPEN exactly where the floor must hold. The read
  uses `GetCurrentPropertyValueEx(ignoreDefaultValue=TRUE)`, because the plain
  form folds "the provider does not implement this" into the property's default
  of `False`: a password box in such a framework would read as safe. Only an
  explicit, supported, live `VARIANT_BOOL` False is treated as plain. The flag is
  deliberately absent from the cached walk — a field can flip plain to password on
  the application's own timer, and a cached verdict then reports `False` while the
  cache holds the cleartext.

  One narrowing, measured: the strict rule applies to text-bearing roles only.
  Explorer's tree provider implements the property for nothing, so 13 of 87 nodes
  came back inconclusive and suppressed the screenshot of an ordinary file-manager
  window. Across ten real windows every inconclusive read landed on a NON-text
  control while every text-bearing control answered definitively, so the floor
  still covers everything that could leak.
- **`BoundingRectangle` is a SAFEARRAY of `[left, top, WIDTH, HEIGHT]`**, not
  `[left, top, right, bottom]` — verified against `GetWindowRect`. It arrives as
  `VT_ARRAY | VT_R8`, so `variant_value` decodes the SAFEARRAY and `_local_frame`
  reads width/height directly; subtracting as right/bottom gave a negative
  (silently dropped) height on any child below its parent's origin and a wrong
  size on the rest, which is the "worse than no rect" failure because a model acts
  on it.
- **One DPI awareness scope must cover walk, capture, confinement and click.**
  UIA rectangles follow the *caller's* awareness rather than always being physical
  pixels (one window measured `(614,223,1424,750)` aware against
  `(491,178,1139,600)` unaware on a 125% display), and a mismatch does not fail
  closed: feeding the unaware coordinate to `WindowFromPoint` resolved a
  **different application**. `windows_ffi.dpi_awareness_scope` is thread-scoped,
  never the irreversible process-wide setter.
- **`PW_RENDERFULLCONTENT` into a 32bpp buffer is the only capture path, and the
  two halves are one mechanism.** The flag renders through DWM, whose surfaces are
  32bpp BGRA, so against a 24bpp DIB it returns 1 and writes **nothing** — the flag
  is silently disabled and every capture falls through. Measured across depth × flag
  on two windows: a WinForms window yielded 1 distinct byte value at 24bpp against
  180 at 32bpp, and a Chromium window yielded 1 distinct value for *every* other
  combination and a full frame only at 32bpp with this flag. Neither half is a
  pixel-format preference, and a code reading shows nothing wrong with either.

- **There is deliberately no `BitBlt` fallback.** A window DC is a view onto the
  **screen**, not onto the window's backing store, so `BitBlt` from it copies
  whatever occupies those pixels — including an overlapping window belonging to an
  application the caller was never authorized for. That is the same confinement
  `apps_windows.hwnd_owns_point` enforces for the pointer, broken on the
  *observation* path where the secure-field floor cannot help: that floor walks the
  TARGET's tree, so a password box owned by the window on top is not something it
  can see, let alone suppress. A blank frame is a degradation; a frame containing a
  third party is a disclosure, so the two are not interchangeable fallbacks.

- **The buffer is sized to what the window RENDERS, not to its DPI-aware rect.**
  `PrintWindow` asks the window to draw itself in *its own* coordinate space, so a
  DPI-unaware window renders at its logical size no matter what the caller's
  awareness is: one drew 620×392 into both a 620×400 buffer and a 775×500 one,
  leaving the aware-sized buffer with a black L-shaped margin and an image that no
  longer maps linearly onto the window rect the element frames are expressed in. A
  DPI-aware window fills whichever buffer it is given (1296 unaware, 1620 aware).
  `windows_ffi.window_render_scale` is the ratio — the window's DPI over the
  monitor's, **both read inside the same awareness scope**, since `GetDpiForMonitor`
  is itself virtualized and reading it unaware collapses the ratio to 1.0.

- **A capture that reports success can still be blank.** `PrintWindow` returned
  success over a uniform buffer on WindowsTerminal, whose swapchain surface is not
  in the DC being copied. The pixels are validated and a uniform frame is a failed
  capture, never relayed — a blank rectangle is worse than no image, because the
  model would reason about it as if it were the application.

  `_has_content` compares whole **pixels**, not individual bytes, and that is
  load-bearing at 32bpp: opaque grey is `20 20 20 FF`, so ONE repeated pixel yields
  two distinct byte values and a byte-wise threshold of two passes a blank window on
  its **alpha lane alone**. Comparing pixels also dissolves the channel question —
  every channel is part of the compared value, rather than depending on a probe step
  landing on the one that differs.

  **Alpha is excluded from the comparison.** GDI leaves it undefined on the
  `PrintWindow` path (a fresh DIB section is zeroed and what DWM writes there is
  unspecified), so a verdict that reads it depends on a byte no renderer promises —
  in both directions: uniform alpha over varied colour is real content, and varying
  alpha over uniform colour is not.

  The frame's WIDTH is required to locate a pixel boundary, and it also excludes
  scanline padding. At 24bpp each row pads to a DWORD and those pad bytes are `0x00`
  while the pixels are uniform, so a raw distinct-byte count passed a genuinely blank
  frame on **694 of the 1245** padded widths in 300..1959, including a real 1005×733
  window. A 32bpp row is inherently aligned, so there is nothing to skip at the
  current depth; the exclusion stays because it is what makes the function correct for
  any depth rather than only this one.

- **The encoded frame is MIRRORED to the dashboard live view**, as on macOS —
  `screencast.emit_snapshot_frame` is platform-agnostic and owns all three
  suppressions (no published surface scope, a secure window, a withheld screenshot
  channel), so without the call the panel is simply blank on Windows. It relays the
  already-downscaled bytes rather than capturing again, does not block (the POST runs
  on a daemon thread), and is wrapped anyway: a decorative mirror must not be able to
  turn a successful observation into a failed tool call.

- **`max_px` and `quality` are clamped at the encode**, matching
  `capture_macos._encode_window`. The MCP schemas validate agent input, but this path
  is also reachable from config, where a zero or negative budget yields a degenerate
  image and an unbounded one yields a frame far larger than any consumer expects.

  Screenshots are locked down in BOTH layers. The directory grant is now
  **inheritable** (`platform_compat.make_owner_only_dir` routes through
  `restrict_dir_to_owner`, which emits `(OI)(CI)`), so a newly created frame does
  inherit the owner-only DACL — but inheritance governs only what is CREATED from
  that point on, and Windows grants Bypass Traverse Checking to Everyone by
  default, so a frame that already exists keeps its own DACL and stays reachable
  through the tightened parent. The directory is created through
  `platform_compat.make_owner_only_dir` (a bare `mkdir(mode=0o700)` is inert on
  Windows, where access comes from the DACL) and each frame is still tightened as
  it is written, which is what covers the already-exists case.

- **The `press_key` grammar is a platform-free seam, and both halves canonicalize.**
  `keymap.parse_spec()` returns a `KeySpec` of abstract names, which `tools._perform`
  validates in the dispatch chokepoint so an unknown key or modifier is refused
  before any driver synthesizes it. A Windows `VK_*` table is keyed on canonical
  names — `KEY_ALIASES` for keys, `MODIFIER_ALIASES` for modifiers — and both
  mappings exist because **the failure mode is a wrong keystroke that succeeds**,
  not a refusal. Two traps are pinned by tests:
  **`cmd`/`command`/`super`/`meta` canonicalize to `ctrl`, not to the logo key** —
  the mapping follows the caller's INTENT rather than the key's name. What a model
  means by `cmd+s` is Save, and on Windows that is Ctrl+S; sending Win+S opens
  Search, leaves the document unsaved, and the close that follows loses the edits.
  `win` is the one spelling that names the physical logo key and stays itself, so
  `win+d` still reaches Show Desktop. macOS is unaffected — `parse_key` reads
  `MODIFIERS`, where all five are `FLAG_COMMAND`.
  `KEYCODES` is a macOS table, and macOS shares one `kVK_*` code between
  `delete`/`backspace` and between `help`/`insert`, so canonicalizing by keycode
  identity alone folded `delete` onto `backspace` — a Windows table built on that
  would send `VK_BACK` for `press_key("delete")` and leave `VK_DELETE` unreachable;
  and a spec's modifiers must be normalized too, or a table testing
  `"ctrl" in spec.modifiers` silently drops the modifier from `control+c` and sends
  a bare `c`. An implied shift (`$`, `A`) is folded into `modifiers`, so
  `parse_spec("A") == parse_spec("shift+a")`. What `win` MEANS stays a per-platform
  decision — it is canonical rather than folded onto `cmd`, since macOS's shared
  `FLAG_COMMAND` is that platform's answer and not a cross-platform fact.
- **Frames are window-local or absent, never unlabelled.** `_local_frame` returns
  `None` when the window origin is unavailable, matching `snapshot_macos`: a
  consumer cannot tell a window-local `(12, 40)` from a screen-absolute one, and the
  screenshot the model may also be reading is a crop of that window.
- **`selected` is `SelectionItem.IsSelected` (30079), not `HasKeyboardFocus`.**
  Focus is already published as `ElementRec.focused` plus the `<focused>` marker, so
  sourcing the trait from focus reported one signal twice while a genuinely selected
  but unfocused row carried no trait — making "which row is selected?" always name
  the caret's element.
- **No COM call carries a duration bound, and that is a platform limit rather than an
  omission.** `macos_ffi` caps every AX call at `AX_MESSAGING_TIMEOUT_SECS` (2.0s)
  because ctypes releases the GIL for the duration of a C call, so an unresponsive
  target parks the calling worker with no interruption path — not even a signal handler
  runs. The hazard is identical on Windows: a provider that pumps a modal inside
  `Invoke` retires one pooled worker until that dialog closes. UIA's equivalent knob is
  `IUIAutomation2::put_TransactionTimeout`, and this client cannot reach it — measured
  on Windows 11, `QueryInterface` for `IUIAutomation2` returns `E_NOINTERFACE` from both
  `CLSID_CUIAutomation` and `CLSID_CUIAutomation8`, while that same `CUIAutomation8`
  returns plain `IUIAutomation` with `S_OK`. So the class is present and only the
  derived interface is absent. What contains it is the bounded executor: one verb is one
  call, so a wedged provider costs a worker and not the loop. A watchdog that abandons
  the call would be worse than nothing — an abandoned COM call still holds its
  apartment, so the thread never returns and the "timeout" only hides the leak.

- **The window list is ON-SCREEN, not merely `IsWindowVisible`.** That flag means "not
  explicitly hidden", so a minimized window (parked at -32000,-32000) and a
  **DWM-cloaked** one (a suspended packaged app, or a window on another virtual
  desktop) both pass it. Measured on one desktop: 4 of 12 enumerated windows were
  invisible to the operator, and the consequences were not cosmetic — the renderer and
  the tool descriptions both promise "on-screen windows", a cloaked window's published
  origin overlaps other applications' real pixels, and the capture returned a full
  bitmap of a window nobody could see. `windows_ffi.window_is_on_screen` adds
  `IsIconic` + `DWMWA_CLOAKED` + a zero-area check, the equivalent of the macOS list's
  `kCGWindowListOptionOnScreenOnly` (which excludes other Spaces the same way). It
  fails **open** on an unreadable cloak attribute, since `DwmGetWindowAttribute` is
  absent without DWM composition and treating "cannot ask" as "not on screen" would
  report an empty desktop.

- **A secure node is never elided**, even when its role is otherwise droppable.
  `secure` flips `has_secure`, which suppresses the screenshot; eliding the node too
  left a window with no tree AND no pixels, explained as "this window contains a secure
  (password) field". That is reachable with no password at all — `Custom` is both an
  elidable role and one where an inconclusive secure read fails closed, so a
  bridge-less Java/Qt app or a game canvas produces exactly it (reproduced:
  `elements=0`, `has_secure=True`). The floor is unchanged; the node stays visible so
  the suppression has a stated cause. **Both walks must pass `secure=`** — the
  numbering `build_snapshot` publishes and the one `addressed` re-derives have to
  match, or an index resolves to a different control.

- **`ElementRec.actions` publishes the DRIVER's vocabulary**, sourced from the cached
  `Is<Pattern>PatternAvailable` properties. The shipped computer-use skill tells the
  model to read this list before choosing a call, so leaving it empty made that
  channel dead on Windows while it worked on macOS — the model had to attempt a verb
  to learn whether the element supports it, and a refusal is indistinguishable from
  having addressed the wrong element. Cached rather than probed: `GetCurrentPattern`
  is a cross-process round trip PER element, exactly the cost the cached walk exists
  to avoid, while these ride along in the walk that already happened. The names are
  `perform_action`'s own (`press`, `toggle`, …), never UIA's pattern names, and
  `WINDOWS_SUPPORTED_ACTIONS` in `types.py` is the single tuple both ends read — a
  name published but not served is a refusal the model was invited to trigger.
  Verified against `GetCurrentPattern` on real controls: a `Button` reported Invoke, a
  `CheckBox` Invoke + Toggle, a `ListItem` Invoke + SelectionItem.
- **`expanded` is `ExpandCollapseState` (30070)**, the counterpart of the macOS
  walk's `AXDisclosing` read: without it a model cannot tell an open tree node from a
  closed one and spends a turn expanding what is already open. Read TRI-STATE — only
  an explicit `Expanded` publishes the trait, because `PartiallyExpanded` is not open
  and `LeafNode` means the control cannot expand at all, so either would misdescribe
  what a collapse would do. Verified on a real `TreeView`: two nodes read `Collapsed`
  and `Expanded` while every non-expandable element in the same window (buttons, the
  title bar, an `Edit`) answered the not-supported sentinel.

**Input works, and its shape is dictated by one platform fact: Windows has no
per-process input delivery at all.** `SendInput` carries no hwnd and no pid, so
unlike `CGEventPostToPid` there is no way to give one application a mouse event
without moving the operator's cursor, or a keystroke without taking their focus.
Every asymmetry below follows from that:

- **Element-addressed actions are the safe form, and the only one `auto` resolves
  to.** A UIA pattern method needs neither the pointer nor focus — the provider
  performs it inside the target — so a press, toggle, select, expand or `SetValue`
  has no effect outside the control it names. The ladder is by SPECIFICITY:
  `Invoke`, then `Toggle` / `Select`, then `LegacyIAccessiblePattern.DoDefaultAction`
  last, because that one performs whatever the provider nominated. **A rung is
  tried only when the previous pattern is ABSENT, never when it was present and
  FAILED** — falling onward from a failure performs a different gesture than the
  one requested, and on a checkbox that is a state flip nobody asked for.

  **The winning rung is NAMED in the result** (`pressed element 3 via Toggle`), as
  macOS names the AX action that succeeded. "pressed element 3" alone hides what the
  call did — a press that landed on `Toggle` flipped a checkbox — and naming it also
  tells the model which pattern this element answers, so the next turn does not
  re-derive it. Measured usefulness: a WinForms `CheckBox` answers `Invoke`, not
  `Toggle`, which nothing in the tree advertises.

  **`capslock` is refused as a modifier here**, though it stays in the platform-free
  vocabulary because macOS serves it. macOS models it as `FLAG_ALPHA_SHIFT`, a
  per-event flag that changes no machine state; `VK_CAPITAL` down/up **toggles a
  machine-wide lock** whose flip OUTLIVES the call, so `capslock+a` would capitalize
  everything the operator types next — and unlike every other modifier, releasing the
  key does not undo it. The refusal names `shift+<key>` as the alternative.

  Every rung is a LEFT-button activation, so **a non-left button on the element path
  refuses.** UIA has no pattern that opens a context menu — no `AXShowMenu`
  equivalent and no ShowMenu pattern at all — so the ladder could only activate the
  control, which is a different action than "open the context menu". This is the same
  rule as macOS's right-button ladder (`AXShowMenu` only, never falling back to a
  press), reached from the opposite direction: macOS has an action to prefer, Windows
  has none to offer, and both refuse rather than substitute. The button IS honoured
  on the coordinate path, where a real right-click is what `global` already means.
- **The pointer path is opt-in by name.** `auto` + coordinates **refuses**, because
  the only coordinate route here moves the real cursor and `auto` must never resolve
  onto a pointer-moving method. `app_post` and `sky_click` refuse BY NAME rather
  than downgrading onto that same route.
- **A drag must name `global` explicitly.** `DragRequest.method` defaults to
  `app_post`, the macOS app-scoped route — and `moves_pointer` is `False` for it, so
  the SEL audit and the cursor-motion overlay both skip a call carrying that default.
  Windows has only the real-cursor route, so accepting the default would warp the
  operator's pointer on a gesture nothing recorded as pointer-moving. Refusing keeps
  `moves_pointer` a true statement about what actually happens, which is what every
  gate upstream reads.
- **A truncated `SendInput` batch is not a failed action, it is stuck hardware.**
  `SendInput` returns how many records it accepted and can stop early — a lower-level
  hook, or UIPI blocking the injection mid-batch — and it returns **normally**, so
  nothing raises and a discarded count is invisible. Two halves, and both were needed:

  * **The PRESS batch** is compared against its length. A batch cut between a
    button-down and its button-up leaves the operator's own mouse button physically
    held, and nothing later un-holds it: every subsequent motion of their hand becomes
    a drag, which on a file manager moves their files. `post_mouse_click` and
    `post_mouse_drag` release regardless and then raise, because a silent partial
    delivery otherwise reads as a successful click — and for a drag, as a drop that
    landed somewhere it never did.
  * **The RELEASE batch is itself verified**, via `_send_release`. Submitting a release
    in a `finally` covers an *exception* and not a *truncation*, so a rejected
    modifier key-up left Ctrl held while the call reported success — which alters every
    keystroke the operator types next. It resubmits the unaccepted tail
    (`SendInput` accepts a prefix, so the tail is exactly `records[accepted:]`), and
    raises after a bounded `_RELEASE_ATTEMPTS` rather than looping forever against a
    permanently-blocking hook. All four release sites — the chord, the click's two
    paths, and the drag — route through it.

    The drag passes **`atomic=True`**, and must. Its release batch is
    `[move(end), button_up]`, whose records are not independent: the move is what AIMS the
    up. A tail-only retry after a partial acceptance resends the bare `button_up`, which
    carries no coordinate and so releases wherever the cursor has got to — the drop lands
    outside the window `hwnd_owns_point` authorized, which is the exact defect the aimed
    release exists to prevent. `atomic` resends the whole batch instead; re-sending the move
    is idempotent, because a cursor position is state rather than an edge.

  In the drag the release runs in `finally`, so a failed release raises from there and
  supersedes whatever the press raised. That precedence is deliberate: a held button is
  worse than a failed drag and is the condition the operator has to act on.
- **Every pointer gesture is confined before it is sent**, and a drag confines BOTH
  endpoints — the release is where a drop lands. A partly-confined drag sends
  nothing at all: a press that already happened is a stuck button, and any later
  motion becomes an unintended drag.
- **The keyboard verbs take focus and say so**, and a focus failure REFUSES rather
  than typing anyway. The policy check cleared the element the model ADDRESSED, so
  keystrokes delivered to a focus that did not move can reach the password field
  that check just refused.
- **`perform_action` takes a CLOSED vocabulary** (`press`, `toggle`, `select`,
  `expand`, `collapse`), each mapped to one specific pattern method. Forwarding the
  caller's string to `LegacyIAccessiblePattern` would be an un-gated write path:
  that interface also exposes `SetValue` and sits on nearly every node of a real
  Win32 tree. **Its `SetValue` slot is deliberately not bound anywhere in the
  package**, which is the durable half of the control — the vocabulary can be
  widened by a later edit, an unbound vtable slot cannot be reached at all.
- **The secure-field floor is re-read LIVE at the driver**, not inherited from the
  snapshot the model was shown: a field can flip plain to password on the
  application's own timer in between, and the driver is reachable without the
  dispatch chokepoint (`kirocrew computer call`). `tools._SECURE_TARGET_TOOLS` also
  widens the chokepoint's own check to `perform_action` and `scroll`, so the floor
  holds for every backend rather than only this one.

**Pattern vtable slots were measured, not transcribed — and a plausible value is
not enough.** Each interface's layout was fixed by probing a read-only getter in a
subprocess (a wrong slot is an access violation, so a crash is data) and requiring
TWO independent signals: a value whose SHAPE could only come from the method named,
and a neighbouring slot that answers `E_INVALIDARG`/`E_FAIL`.

That rigour is not ceremony. **A wrong action slot does not necessarily fault** — it
can land on a neighbour that returns `S_OK` while doing something else, which no
return-code check catches. Two interfaces here did exactly that:

- `ScrollPattern.Scroll` at slot 4 is `SetScrollPercent`. It answered `S_OK` for
  every call while the panel never moved, so the driver reported "scrolled 2
  page(s)" over an unchanged window and a model would scroll forever. Found only by
  reading the panel's own scroll percentage back and seeing it pinned; the correct
  slot is 3, anchored by slot 8 returning `20.13` (a ViewSize FRACTION, so not a
  percent) and slot 10 returning `1` for `VerticallyScrollable` (a `VARIANT_BOOL`,
  so not a double).
- `LegacyIAccessiblePattern.DoDefaultAction` at slot 4 is `Select`. The correct slot
  is 3, anchored by `get_CurrentRole` (slot 10) returning `ROLE_SYSTEM_PUSHBUTTON`
  on a real Button and `get_CurrentDefaultAction` (slot 15) returning `'Press'` on
  the same element.

`test_computer_use_windows_input.py` pins both as numbers with their anchors, so a
future edit that "tidies" a slot back to the header's apparent order fails.

A minimized window is refused up front with its own reason. It is still `IsWindow`
and still `IsWindowVisible` (that flag means "not explicitly hidden"), and its rect
is parked off-screen at `-32000, -32000`, so the capture SUCCEEDS over a uniform
buffer and the blank-frame gate rejects it with no cause anyone can report. The
tree is still returned — the UIA tree of a minimized window reads perfectly well.

`kirocrew computer doctor --json` is what the gateway shells for the Settings
permission rows — a short-lived subprocess, deliberately NOT an in-gateway ctypes
call, so a native fault cannot take the gateway (and with it cron, Slack and the
dashboard WS) down.

### An app's own popups are not addressable surfaces

A modern Windows app publishes transient popups — a tooltip, a flyout, a menu surface —
as top-level windows carrying its own image name (WinUI spells them
`Microsoft.UI.Content.PopupWindowSiteBridge`). They pass every other filter, so they
become a second `list_apps` entry indistinguishable by name from the real window, and
`resolve_app` returns the first match.

Measured on MS Paint: a 97x52 `PopupHost` was listed beside the real 1536x960 document
window, so `resolve_app("mspaint")` resolved to the popup. Every coordinate gesture
aimed at the document was then refused by `hwnd_owns_point` — correctly, since the point
really was outside the 97x52 rect it had resolved to — and the refusal points at bounds
that never change, so the retry cannot terminate. `_TRANSIENT_CLASS_PREFIXES` drops
them, with one exception: **a popup whose title trips the denylist is still listed**, so
it can be refused. Dropping it earlier would let an innocuous same-handle sibling take
its slot, which is the overwrite the denied-title guard exists to prevent.

`find_window_for` — the launch verb's lookup — matches the executable stem first and the
window TITLE second, and the second tier is not redundant: `_app_ref` deliberately puts
the title in both `name` and `bundle_id` for a window fronted by `ApplicationFrameHost`,
so for a packaged app the catalog's stem and the published identity never agree. Without
the title tier the already-running check never fires (the model opens a second copy) and
the post-launch poll burns its whole timeout reporting "no window appeared" for a window
that is on screen.

### Permission probes are ADVISORY, never a gate

`AXIsProcessTrusted()` + `CGPreflightScreenCaptureAccess()` only. Never
`CGRequestScreenCaptureAccess` — it pops a system dialog from a background
process. macOS attributes a TCC grant to the **responsible parent** of the
process tree, so both probes read `missing` while a full-fidelity capture
succeeds; that was observed live. The probe returns a `responsible_hint` naming
the process a user should actually grant (the packaged app, or the terminal that
launched a dev gateway), the Settings copy says "Not detected does not always
mean unavailable", and the feature is **never** gated on the result. Ad-hoc
re-signing anything in the chain can void a grant — the same mechanism that
permanently broke the reference bundle's Accessibility 3/3 times — so
`packaging/resign-macos-libs.sh` is a known hazard for this feature.

### The grant shortcuts MUST use `window.open`, not `location.href`

Each non-granted permission row offers an **Open System Settings** button that
targets a macOS System Settings deep link (`SETTINGS_URL_*` in `permissions.py`;
the frontend mirrors the two constants). It must be handed off with
`window.open(pane, '_blank', 'noopener,noreferrer')` —
`openSystemSettings()` in `ComputerUsePanel.tsx` — never by assigning
`window.location.href`.

The reason is CSP, and it is invisible in a browser tab: the dashboard renders
inside an **instance `<iframe>`** (`InstancesViewport`), and a *frame* navigation
is governed by the `frame-src` directive — declared explicitly in `_BASE_CSP`
(`dashboard/server.py`) as `frame-src 'self' blob: https://*.cloudfront.net …`,
a loopback/cloudfront allowlist that names no custom scheme.
Assigning `location.href` to an
`x-apple.systempreferences:` URL is therefore refused with
`ERR_BLOCKED_BY_CSP` before it ever reaches LaunchServices, so the button is a
**dead click in the packaged desktop app** while working fine from a top-level
page. `window.open` is a new top-level request instead, which is not subject to
`frame-src`.

In Electron that `window.open` arrives at the main process's
`setWindowOpenHandler`. That handler used to allow any **same-origin** URL in-app,
forward cross-origin `http:`/`https:` to the browser, and silently deny
everything else — so the deep link died there too.
`electron/external-scheme.js` owns that decision now:
`classifyNavigation()` returns `allow` (same-origin, in-app), `external`
(cross-origin web + an **allowlisted** non-web URL → `shell.openExternal`), or
`block`.

The non-web allowlist (`EXTERNAL_URLS`) matches **whole URLs, exactly** — the two
`SETTINGS_URL_*` panes — not the `x-apple.systempreferences:` scheme. A
scheme-granular rule would admit unbounded attacker-chosen payloads into
LaunchServices, and that is reachable rather than theoretical: LLM-authored widget
and artifact content renders in iframes carrying
`sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"`, no CSP
directive constrains a `window.open` target's scheme, and remote-instance frames
share this same handler — so model-generated JS could otherwise pop *any* pane
(Sharing → Remote Login, Configuration Profiles) beside agent-authored text
telling the user to enable it. Exact matching also removes a parser-differential
class: the verdict is computed from `new URL(...)` (WHATWG lowercases the scheme,
strips tabs/newlines inside it, resolves `..`) while the **raw** string is what
`shell.openExternal` re-parses with NSURL/CFURL; requiring raw equality means the
validated and forwarded values cannot diverge. Because it is exact-match, the
constant must stay byte-identical to `permissions.py` and the panel's `PANE_*`
mirrors — `external-scheme.test.js` reads both real files and asserts agreement,
since a drift is a silent dead button.

`file:` is excluded by construction (handing an arbitrary local path to the OS is
a disclosure/execution vector, not a navigation), an unparseable URL fails closed
to `block`, and an unusable app origin never *promotes* a cross-origin URL to
same-origin — including an **opaque** one, where `new URL()` succeeds and reports
the literal origin `"null"` that would otherwise compare equal to a foreign
opaque origin. `shell.openExternal` returns a **rejecting** Promise when the OS
has no handler, so the hand-off swallows the throw, the rejection, *and* a
throwing log sink; the handler body is guarded end-to-end and fails closed to
`deny`, because a dead grant shortcut must never take the main process down.

That handler is the single gate for **every** `window.open` in the dashboard, not
just this button, so it checks **same-origin before protocol** — the ordering the
inline handler it replaced used. This matters for `blob:`, which *inherits* the
creating page's origin: `WidgetFrame`'s "Open in new tab" opens a
`URL.createObjectURL` wrapper document and needs a real window object, so a
same-origin blob must classify as `allow`. A protocol-first ordering demoted it to
`block` and turned that button into a dead click — the same silent-failure shape
as the bug above. Origin-inheriting schemes are checked for same-origin ONLY and
never appear in the external allowlist, so `blob:` can never reach
`shell.openExternal`. `external-scheme.test.js` pins this with a parity table
asserting that every pre-existing URL shape keeps its old verdict and that the
System Settings deep link is the *only* changed one.

Both halves are pinned by tests (`ComputerUsePanel.test.tsx`,
`electron/test/external-scheme.test.js`) because the failure mode is a silent
dead button that only reproduces in a packaged build.

---

## The CLI (`kirocrew computer`) — and what is deliberately not ported

`computer_use/cli.py`. Three verbs, hand-rolled dispatch rather than argparse
subparsers (the parent CLI forwards `REMAINDER`). Full command reference in
[cli.md](cli.md).

| Verb | For | Gated on the primary enable? |
|---|---|---|
| `doctor [--json]` | platform support + enable state + the advisory TCC probe | no — it *reports* the enable, and returns no desktop content |
| `apps` | the on-screen application list | **yes** — see below |
| `call <tool> [k=v …]` / `call --calls '[…]'` | run one tool, or a sequence in ONE process | **yes**, and everything else too |

`apps` is gated, and used not to be. The original reasoning — "this is the operator
running a diagnostic in their own terminal, and the enable exists to stop the
*agent*" — does not hold, because **the agent can run this command with bash**. As
written it was an ungated read of every window TITLE (document names, paths, and
whatever a terminal put in its title) that worked with the feature disabled, in an
unattended cron session, and under a policy banning computer use outright. It now
runs `computer_list_apps` through `dispatch_tool` like everything else, so the app
denylist and the observation ceiling apply. `doctor` remains the ungated way to find
out *why* the feature is off: it reads the keystone and the TCC state only, never the
window list.

### `call` is a harness, not an eleventh tool

`call` runs the existing tools through `tools.dispatch_tool` — **the same ordered
chokepoint an agent call traverses**. The fail-closed primary enable, the
audit-only `gate.require_computer_use`, the app denylist, index freshness, the
secure-target refusals and the observation ceiling all apply, so `call` cannot see
or do anything the agent could not. That is precisely what makes it a faithful
reproduction tool rather than a debug backdoor, and it is why the implementation
goes through `tools` rather than reaching into `service` (a test asserts that over
the AST — a future "skip the overhead" edit would otherwise stay green while
dropping governance on the floor).

One consequence, stated rather than left to be discovered: the session key is
always the attended `cli_chat` surface (`sel._infer_source` → `cli`), used for the
SEL audit record. There is no identity proof and no separate diagnostics opt-in —
both existed only to satisfy the unattended-surface refusal, which is gone. `doctor`
remains the diagnostic that works regardless of the enable, because it reads the
keystone and the TCC state and returns no desktop content.

**Why a whole array in one process.** `element_index` values only mean anything
relative to the `computer_get_state` that produced them, and that mapping lives in
a per-process `SnapshotIndex` with a 90s TTL. Two separate `kirocrew computer call`
invocations therefore cannot share indices at all — the second refuses with "no
state for …". `--calls '[{"tool": …, "args": {…}}, …]'` exists so a
snapshot-then-act sequence is reproducible from one command line. It runs
sequentially and does **not** abort at the first error: a reproduction is more
useful whole, since "step 2 was refused and step 3 then hit a stale index" is the
actual story. `--json` emits `[{tool, text}, …]`; the exit code is non-zero if any
reply carries the `Error: ` prefix.

`call` has **no MCP twin**, and that is the MCP-first rule being followed rather
than bent: the rule exists so the model gets a structured tool instead of being
told to shell out, and the model already has all eleven tools. A tool that runs other
tools would let a model launder one per-call gate decision into many.

### Deliberately not ported from the reference implementation

| Reference surface | Why not |
|---|---|
| ~~`sky_click` (a fifth `click_method`)~~ | **Now ported** — see "`sky_click` — the private path, and why it IS shipped" above. Kept in this table as a pointer, because the reasoning that once excluded it (do not depend on private ABI) still governs how it is contained: quarantined in `macos_skylight.py`, never reachable from `auto`, and fully degrading when a symbol is missing. |
| `install-codex-mcp`, `install-claude-mcp`, `install-gemini-mcp`, `install-opencode-mcp`, `install-codex-plugin` | N/A by design. KiroCrew **self-registers**: `kirocrew-computer` is a managed server in `agent.py:_MANAGED_MCP_SERVERS`, auto-written into the agent config and refreshed while preserving user customizations. There is no external host to install into, so an install verb would have nothing to do. |
| `snapshot <app>` | covered by `apps` + `computer_get_state`, and a CLI spelling of an LLM-facing capability is exactly what the MCP-first rule asks us not to add. |
| `turn-ended [--previous-notify]` (a host notify hook) | same intent, different shape: `computer_end_turn` is an MCP tool, so the model drops its own snapshot cache rather than relying on a host lifecycle hook KiroCrew does not have. |

---

## Known limitations

Stated plainly. Each one is real; none is papered over.

### The keystone is the whole security boundary

There is no second plane any more. `hooks.on_tool_call` still classifies a
computer-use title for the ordinary tool/mcp scopes, but it makes no computer-use
decision, and `gate.require_computer_use` only audits. Everything rests on one
fact: **the keystone `computer_use.json` is on `security._SENSITIVE_HOME_DIRS`, so
the agent can neither read nor write it.**

That is a real boundary — it is the same mechanism that protects
`security_policy.json` and the SEL HMAC key, and it is what makes "the agent cannot
turn on its own desktop automation" true rather than aspirational. But it is the
ONLY one, and the consequences should be stated rather than discovered:

* once the operator enables the feature, prompt injection that reaches the agent
  reaches the desktop. There is no per-app, per-action or per-surface ceiling left
  to contain it;
* the one structural defence is that the agent cannot drive KiroCrew's own window
  (`policy.check_app`), so it cannot click the enable itself. If that entry is ever
  removed, the keystone stops meaning anything. It matches THREE signals, and the
  third is load-bearing: the dashboard is also reachable as a **browser tab**, where
  the app identity is Chrome's or Safari's and a bundle/name rule cannot fire at all
  (reviewer finding). `DeniedApp.title_substrings` matches the resolved window title,
  as a substring — the tab title takes a badge prefix (`(3) Kiro Crew`) and popouts a
  `… — Kiro Crew` suffix. And because input is delivered per-PID
  (`CGEventPostToPid`), `apps_macos.list_apps` prefers a DENIED title over an
  innocuous one when one process owns several windows: a dashboard in a background
  tab must refuse the whole browser, not just that window.

  Accepted false positive, stated rather than discovered: any window whose title
  merely contains "kiro crew" is refused — an editor with this repo open, a browser
  reading this PR. The failure directions are not symmetric. Refusing an unrelated
  window is visible and recoverable (the refusal names the target); failing to refuse
  our own dashboard silently hands the agent its own security settings. A title is
  also the only signal available: the driver reads no page URL, and `AXURL` is not
  bound anywhere in the package;
* an agent that can write `~/.kiro/agents/*.json` can stop kiro-cli sending
  permission requests, so the interactive prompt is not a guarantee either. That is
  a pre-existing gap affecting every capability, hardening deferred to its own PR;
* **typing into a terminal window reaches a shell without passing the command deny
  floor.** The 137 `BUILTIN_DENIED_RULES` are matched against a `bash` tool call's
  command string; a `computer_type_text` into Terminal.app is not one, so
  `security.py` sees none of it. The terminal denylist entry that used to cover this
  was removed with the rest of the per-app model (it was incomplete by construction —
  an IDE's embedded terminal never matched it — and it blocked legitimate use on the
  operator's own machine). The sensitive-text scan still inspects what is typed, but
  it is a credential/secret filter, not the command floor. Stated here because it is
  the sharpest edge of the one-opt-in posture.

This is a single-user-machine posture: the operator is trusted with their own
desktop, and the product optimises for the feature being usable rather than for
containing a compromised agent. A deployment that needs the latter should not enable
computer use.

### Other accepted residuals

- **The screenshot directory stays agent-readable.** Persisted JPEGs live in a
  `0o700` temp dir the agent can reach with `fs_read` — the same posture browse
  already ships. Computer use widens WHAT can be in frame (any window, not one
  browser tab). Mitigations: per-window capture only (never full-screen),
  whole-window suppression when any node is secure, ring-trim to 200, and the
  existing `cleanup-temp-screenshots.yml`. This design does not widen the posture
  and does not claim to close it.
- **"No screenshots" is not "no disclosure."** The accessibility tree itself
  leaked real paths, window titles and bundle ids in live probes, and a document
  path inside an `AXTitle` is not a credential so redaction will not catch it.
  `window_titles`, `file_paths` and `element_values` are **not** separately
  governable — the `observations` scope was removed, `apply_observation_ceiling` is
  a pass-through and `permitted_observation_channels` returns every channel — so
  there is no `computer_use.apps` (or any other) row left to narrow. A deployment
  that needs a real bound on what the tree may disclose should not enable the
  feature.
- **Nothing bounds the blast radius, and there is no undo.** Every mutating tool is
  irreversible in the real world: a click that sends an email, a `set_value` that
  changes production config in an authenticated tab. There is no
  `effective = POLICY ∩ PROFILE` evaluation on this path — no module under
  `computer_use/` consults a profile — so `enabled: true` grants **unbounded**
  desktop automation by construction, not as the default of a tunable model.
- **Element-index addressing is inherently racy** — see the honest limit under
  [Index lifecycle](#index-lifecycle-and-its-honest-limit).
- **A ctypes fault ends computer use for the rest of the kiro-cli session.**
  kiro-cli caches `tools/list` once per session. Mitigation is prevention (the
  argtypes tripwire test), not recovery; a per-call fork was considered and
  rejected as disproportionate.

### Enabling restarts the chat sessions (on purpose)

That same `tools/list` cache is why `PUT /api/computer-use/config` calls
`_reset_all_sessions` whenever the enable **flips**. ACP has no
`tools/list_changed` notification, so a session that started while the feature was
off keeps an empty computer-use tool set for its whole life: the operator enables
it, asks the agent to look at a window, and is told there are no tools. Restarting
is the same remedy `POST /api/mcp/sync` already applies when MCP routing changes,
and for the same reason.

**The spec is rebuilt BEFORE the reset, under the config lock, and both of those are
load-bearing.** The enable is also a spec-emission gate ([above](#the-shim-is-not-spawned-at-all-unless-it-can-be-used)),
so while it was off the server was not in `mcpServers` at all. A reset alone would
restart every session into the *same* spec that omits it, and the tools would not
appear until the next gateway start; a rebuild *after* the reset would be equally
broken, restarting sessions into the old spec. Rebuilding first keeps the
user-visible contract — enable, sessions restart, the tools are there — exactly as
it was before the gate existed.

The lock matters because the rebuild READS the keystone and WRITES the spec. Outside
it, two overlapping PUTs interleave: an enable's slower rebuild can land its spec
*after* a later disable's, leaving a spec that mounts — and therefore spawns — the
server the keystone now forbids. Holding `_get_config_lock()` makes read-decide-write
atomic against every keystone writer, so whichever rebuild finishes last is the one
that read the final state. It is a plain reacquisition: the write block has already
exited its own, and `rebuild_agent_config` never takes this lock.

A rebuild failure never fails the SAVE (same rule as the reset: the write already
landed and was audited), and the fallback is the old behaviour of the surface
appearing on the next cold gateway. Pinned by
`test_computer_use_api.py::TestEnableRestartsSessions`.

Deliberately narrow, so a restart is never gratuitous:

- only on the `enabled` key, and only when the value actually CHANGED — a no-op
  re-save must not tear down the operator's session;
- never for the budget knobs (`max_tree_nodes`, `screenshot_max_px`, …), which are
  read per call;
- a restart failure never fails the SAVE. The write already landed and was audited;
  reporting failure would be a lie, and the fallback is simply the old behaviour
  (the new tool surface appears on the next cold session).

The response carries `sessions_reset` so the panel can EXPLAIN the restart — an
unexplained session reset reads as a crash. Pinned by
`test_computer_use_api.py::TestEnableRestartsSessions`.
- **The live mirror is SPARSE, not a video feed.** [The live view (PiP)](#the-live-view-pip)
  is a relay over the screenshots the model already read, so the panel updates once
  per `computer_get_state` with `attach_screenshot` and shows nothing at all during a
  run of pure actions. That is deliberate — the alternative is a second capture the
  agent did not ask for — but it means the panel is not a substitute for watching the
  screen, and the Settings copy must not imply otherwise.
- **Coordinate clicking has no pixel→element verification.** Unlike element
  addressing — where the fingerprint check turns an index into a real assertion —
  a coordinate names a point the OS delivers to whatever is there *now*. There is
  no drift check possible for it, so a coordinate click can land on a control that
  moved after the screenshot. Mitigation is the model's own: prefer
  `element_index`, and re-`computer_get_state` after anything that reflows a
  window.
- **`click_method: "global"` inverts the "the pointer never moves" property.** The
  model has to name it (`auto` never resolves onto it) and every use is audited under
  its own `tool_kind`, but when it does, the agent can aim the operator's physical
  cursor at anything on screen — including UI the app-scoped path deliberately cannot
  reach. That reach is the point of the path; there is no longer a separate opt-in
  gating it.
- **A drag cannot be verified either, and it is coordinate-only.** No accessibility
  action expresses a sweep between two points, so `click_method: "accessibility"` is
  refused for it rather than approximated.
- **`_CU_ACTION_CLASSES` must stay in sync with the tool list.** A tool added
  without a table row is classified `("mutate",)` — fail-closed in both
  directions (it can never satisfy an `@observe` allow-list and IS caught by an
  `@mutate` deny), but it also means a *read* tool added without a row will
  needlessly prompt. The coverage tests enumerate the registered tool set; the
  next author adds the row.
- **The shell plane is a separate plane.** `osascript` / `cliclick` / `xdotool` /
  `screencapture` typed into a Bash tool are `commands`-scope items, never
  re-parsed into GUI sub-effects, and the **web terminal PTY**
  (`dashboard/handlers/terminal.py`) contains no deny-floor or governance call at
  all — it is an operator-only, ungoverned plane today. `playwright-cli screenshot`
  is a second pixel channel and lands on this same plane: it is a shell command,
  so an `mcp`-scope deny cannot reach it. None of these are covered by any
  `computer_use.*` scope, and the spec says so rather than implying coverage.

---

## Files

| File | Purpose |
|---|---|
| `computer_use/types.py` | Every constant + frozen dataclass; dependency-free and platform-free |
| `computer_use/keymap.py` | The platform-free spec grammar (`parse_spec()` → `KeySpec`, `KEY_ALIASES` / `MODIFIER_ALIASES` and their canonicalizers) over macOS's Carbon keycodes + CG flag masks (`parse_key()`) |
| `computer_use/policy.py` | The one retained app refusal (KiroCrew's own window) + the operator's allow/deny lists, secure-target + text refusals, the click-target/method/button refusals + `resolve_click_method`, `redact_result` |
| `computer_use/render.py` | Tree/app-list rendering, `fingerprint`, secure placeholder |
| `computer_use/index.py` | `SnapshotIndex`: TTL, cap, `resolve`, `end_turn`, drift message |
| `computer_use/enable_state.py` | The keystone primary enable + the operator's app allow/deny lists (read fail-soft to off) |
| `computer_use/backend.py` | `ComputerUseBackend` ABC, `UnsupportedBackend`, registry, the one platform branch |
| `computer_use/gate.py` | The SEL audit of every call and every real-pointer gesture, plus the pass-through shims (`apply_observation_ceiling`, `permitted_observation_channels`) the renderers still route through |
| `computer_use/service.py` | The single dispatch chokepoint (`act()`), synchronous |
| `computer_use/windows_driver.py` | `WindowsBackend`: observation + all seven input verbs — the UIA pattern ladder, pointer confinement, verified focus, and the closed `perform_action` vocabulary |
| `computer_use/windows_ffi.py` | The ONLY module touching Windows-native code: the UIA COM client, the one vtable-slot table, VARIANT/BSTR marshalling, the fail-closed secure read, the bounded walk, the DPI scope, `window_render_scale`, window enumeration |
| `computer_use/apps_windows.py` | Window-list app enumeration keyed on the top-level HWND (a pid fronts many apps), the transient-popup filter, `find_window_for` (the launch verb's non-raising lookup) + `hwnd_owns_point`, the confinement predicate |
| `computer_use/snapshot_windows.py` | Iterative bounded UIA walk, UIA role vocabulary, window-local frames, truncation flags |
| `computer_use/capture_windows.py` | `PW_RENDERFULLCONTENT` into a 32bpp DIB sized to what the window renders + GDI+ JPEG encode, with pixel validation because a blank capture reports success. No `BitBlt` fallback: it reads the screen |
| `computer_use/launch_windows.py` | `computer_launch_app`'s Windows resolver: the `App Paths` catalog (read including the writable hive) verified against protected install roots + a basename match, the shell-target refusal, and the no-arguments spawn |
| `computer_use/launch_macos.py` | The macOS resolver: `.app` bundles under the conventional roots, handed to `/usr/bin/open -a` (pinned absolute) with no document and no arguments |
| `computer_use/linux_driver.py` | Typed refusal + the implementation plan |
| `computer_use/macos_ffi.py` | The ONLY module touching ctypes: `_FN_SPECS`, structs, binder, CF hygiene, key/scroll/mouse event synthesis |
| `computer_use/apps_macos.py` | Window-list app enumeration + pid resolution (never `pgrep`). Bundle `Info.plist` reads honour `security.is_sensitive_path`, so a bundle planted under a protected directory resolves to "identity unknown" rather than being opened |
| `computer_use/snapshot_macos.py` | Iterative AX walk, `AXManualAccessibility` retry, secure detection |
| `computer_use/capture_macos.py` | In-process capture + ImageIO encode + `0o700` persistence + ring trim |
| `computer_use/screencast.py` | Live-view (PiP) relay: `frame_scope`, the three suppressions, `build_frame_payload`, the loopback POST |
| `computer_use/cursor_motion.py` | Cursor Motion PATH MODEL — pure geometry (Bezier + arc + progress spring), plus `drag_points` for a multi-point drag. No ctypes, no AppKit, no config |
| `computer_use/overlay.py` | Gateway-side overlay SUPERVISOR: the `cursor_motion` opt-in, child lifecycle, motion commands. Never raises, never blocks the loop |
| `computer_use/overlay_proc.py` | The AppKit overlay CHILD (`python -m …overlay_proc`). Its OWN ctypes surface — out of process on purpose; `NSWindowSharingNone` keeps it out of screenshots |
| `computer_use/permissions.py` | Advisory TCC probe + `responsible_hint` |
| `computer_use/macos_driver.py` | `MacOSBackend` glue |
| `computer_use/cli.py` | `kirocrew computer doctor [--json] \| apps \| call` |
| `mcp_computer.py` | The thin stdio shim (`kirocrew mcp-computer`) |
| `testing/fake_computer_use.py` | `FakeComputerUseBackend`, shipped in the wheel |
| `dashboard/handlers/computer_use.py` | `/api/computer-use/{config,invoke,frame}` |
| `website/src/pages/settings/ComputerUsePanel.tsx` | Settings → Computer Use |
| `website/src/components/ComputerUseLiveView.tsx` | The floating live view (PiP) panel |
| `website/src/hooks/useComputerUseFrame.ts` | Frame-stream subscription + session-title lookup |
| `src/kiro_crew/builtin_skills/computer-use/SKILL.md` | The agent-facing workflow. **Bundled**, not in the top-level `skills/` dir: `config/prompt.md` tells the model to read it by name, so per AGENTS.md it is load-bearing and must reach every pip/DMG install |

Cross-references: [governance.md](governance.md) for why computer use is
deliberately NOT governed; [security.md](security.md) for the keystone leaf and the
denylist's place in the security model; [config.md](config.md) for the
`computer_use` config section; [cli.md](cli.md) for the commands.
