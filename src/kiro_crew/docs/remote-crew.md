# Remote Crew

A **Remote Crew** is another machine running its own Kiro Crew gateway — a
dev box, an EC2 instance, the server in your closet — that this dashboard can
reach. One dashboard becomes the hub: you switch between your own machine and
each remote one from a strip in the top header, start a session that runs over
there, and search every connected machine's history from one box.

A gateway binds its dashboard to loopback only, so nothing here opens a port on
the remote. The hub tunnels to the remote's loopback port over SSH or AWS Systems
Manager, mints a short-lived dashboard token on the remote, and embeds that
dashboard in the pane. The remote keeps running whether or not you are looking
at it; the hub only adds a way in.

The word *crew* means four different things in this product, and three of them
turn up on this page. [The four things called "crew"](#the-four-things-called-crew)
sorts them out.

## Turning it on

Remote Crew is off by default and is read only at gateway startup, so it
takes a restart:

```bash
kirocrew config set instances.enabled true
kirocrew restart
```

**Settings → Remote Crew** has the same toggle and then tells you a restart
is still pending. With the flag off, that page shows an opt-in card and the
instances API answers `403`. If the page says the feature is enabled but not
active, the flag was set after the gateway started — restart.

## SSH or SSM: which transport

Each instance records a **connection method**. Both of the two you would pick by
hand end at the same place — the remote's loopback gateway — but they get there
differently.

| | **SSH** (default) | **AWS SSM** |
|---|---|---|
| Inbound port on the remote | needs SSH reachable, directly or through a bastion | none |
| What authorizes you | an SSH key your agent holds | IAM — `ssm:StartSession` on that instance, plus `ssm:SendCommand` and `ssm:GetCommandInvocation`, which the token mint runs over. Granting only the first brings the tunnel up and then fails the mint |
| Needed on the hub | `ssh` | AWS CLI plus `session-manager-plugin` |
| Needed on the remote | `sshd` | the SSM agent and an instance role for SSM |
| Revoking access | rotate or remove the key | edit the IAM policy |

Choose **SSH** for a machine you already `ssh` into — a dev host, a home server,
anything with an `~/.ssh/config` entry. It is the shorter path: if
`ssh <host>` works without a password prompt, so does this.

Choose **SSM** for anything in AWS you would rather not expose port 22 on, or
where you want reachability to be a policy decision rather than a key someone
holds. The [EC2 guide](../../../docs/guides/remote-crew-on-ec2.md) walks both
ends of it, and
[SSM vs SSH](../../../docs/guides/cloud-instance-ssm-vs-ssh.md) covers why a
cloud-launched machine is registered with SSM.

A third method, **Fargate**, is set for you when a crew container is launched as
an ECS task. It forwards to the task's chat API rather than a dashboard, so its
card shows a URL to copy and opens no pane.

Two prerequisites hold for the two methods you pick yourself: you can reach the
remote non-interactively (no password prompt), and the remote already has Kiro
Crew installed with its gateway running. Neither applies to Fargate — the
launcher registers that record for you, and the task runs no gateway to reach.

## Adding an instance

**Settings → Remote Crew** is the control plane. It does not embed anything
— that is the switcher's job.

1. Open the panel and add an instance.
2. Fill in the fields:

   | Field | What to put in it |
   |---|---|
   | Name | Any label. It is what the switcher, the pane and every badge call this machine. |
   | SSH host / alias | Exactly what you would type after `ssh` — a hostname, an FQDN, `user@host`, or an `~/.ssh/config` alias. Per-host details like `User`, `IdentityFile`, `Port`, `ProxyJump` stay in your SSH config. |
   | Remote port | The port the remote gateway listens on, `5476` by default. Several instances may share it; the hub allocates each one its own local port. |
   | Token TTL | How long a minted dashboard token lives, `20h` by default. |
   | Remote kirocrew path | Only when `kirocrew` lives somewhere unusual on the remote. |

   An SSM instance takes its target id (`i-…` or `mi-…`) and, optionally, an AWS
   profile name, a region, and the remote POSIX user commands should run as
   (`ec2-user` by default — an Ubuntu image usually needs `ubuntu` here).
   Only the profile **name** is stored; credentials come from the AWS CLI's own
   chain.
3. Click **Connect**. The hub opens the tunnel and mints the token.

The registry file (`~/.kiro/crew/instances.json`) holds connection coordinates
only. No key, password or token is ever written there.

**Editing and removing** live in each row's overflow menu. Changing anything the
tunnel is built from — the host, the port, the transport — drops the live tunnel
first, so the row comes back offering **Connect** rather than quietly forwarding
the old port under a new label. Renaming or changing the TTL leaves a working
pane alone. If you started typing an edit and navigated away, the draft is still
there when you come back, and Kiro Crew refuses to open a second row's editor
while it is unsaved rather than throwing your typing away.

## The switcher strip

The top header carries the switcher. **Local** returns to your own dashboard;
every other entry is one of your machines, each naming its tunnel state in words
beside its status dot. In the desktop app, Cmd/Ctrl plus a digit jumps between
panes in switcher order.

Entries live in a dropdown because the number of machines is unbounded. Any entry
— **Local** included — can be **pinned** from the pin icon on its own row, which
lifts it out of the menu into an always-visible chip beside the trigger. Nothing
is pinned until you pin it, so a one-machine install sees no chip row at all. The
dropdown keeps an aggregate unread badge for every machine not on screen.

The hub keeps the most recently used panes **warm** — tunnel up, dashboard
loaded — and reconnects the rest on demand. A pane that dropped out of the warm
set still has its tunnel; selecting it re-warms it, which looks like a
reconnect because the token is re-minted and the remote dashboard boots again.

Each row also carries badges read off its record: **SSM** or **SSH** for the
transport, and **EC2** when the instance was provisioned by the cloud launcher.

## A session that runs on another machine

**New chat on crew** starts a session here whose turns execute over there. It is
a feature preview: turn on **Settings → Developer → Feature Previews → Chat on
a crew**, then pick it from the new-chat menu and choose one of your connected
machines. Only connected ones are offered.

What you get is a local session — a row in your own sidebar, a local transcript,
local history and search — wearing a chip that names the machine running it. Its
prompts, tools and model all run on the remote, against the remote's workspace
and its agents.

Two things follow from the split:

- **The remote's roster wins.** The session uses the remote machine's agents,
  models and workspaces, not this machine's. Your local default agent is not sent
  over; a name from your roster would mean nothing, or something else, over
  there.
- **The tunnel has to be up to send.** While it is down the session refuses a new
  turn and says it is reconnecting. A turn already in flight keeps running on the
  remote even if the hub restarts — when you come back, the transcript carries an
  explicit note that a restart interrupted it rather than just stopping
  mid-sentence.

Creating one fails outright when the machine is disconnected or running a Kiro
Crew version too far from yours, rather than leaving you a session that cannot
send.

## Another machine's sessions in your list

The opposite direction: sessions a connected machine **owns** can be merged into
this dashboard's Sessions list. Turn on **Settings → Developer → Feature
Previews → Remote crew sessions**. With it off, nothing is fetched at all.

Peer rows are ordered with your local ones by recency and badged with the machine
that owns them. Clicking one opens it **here**: the hub binds a fresh local
session to that remote session and backfills its transcript, so you get a real
local conversation whose turns keep running on the remote. The row keeps the
identity it had, so it re-renders in place instead of appearing twice, and it
drops out of the peer rows once adopted.

A peer row deliberately does less than a local one, because the other machine —
not this one — owns the session:

| Not on a peer row | Why |
|---|---|
| Rename, close, pin | They change the session, which lives on the other machine. |
| Folders and drag | Filing is local bookkeeping about local rows. |
| The `⋯` group actions | Same reason: they act on sessions this machine owns. |
| The board view | It stays local, and reports how many rows it filtered out. |

A machine that cannot say which memory mode a session runs in is refused rather
than guessed at, so an adopt never quietly crosses a privacy boundary you set
over there.

Only **live** sessions appear here. The other machine's closed ones are reachable
through search.

## Searching every machine's history

With at least one machine connected, both search surfaces — the ⌘K palette's
Sessions tab and the sidebar's Older Sessions search — answer from your own
gateway *and* every connected one. With none connected they stay local, so a
single-machine install pays nothing for the feature.

Results interleave by rank: position one is your best local hit, position two is
the best hit from the next machine, and so on. Scores are never compared across
machines, so no machine's results can crowd out another's. A machine that does
not answer in time is reported as not searched rather than silently dropped.

A remote result is badged with its machine's name, and:

- **Clicking it switches to that machine's pane**, reconnecting first if needed.
  The transcript lives over there, so this is how you get to it.
- **Delete is hidden.** It would target a local session file, which is not this
  one.
- **⌘Enter** (open in the local split grid) does nothing on a remote row.

If the federated search fails for any reason — including the feature being off —
search falls back to your local sessions, which is always the floor.

## The four things called "crew"

The product's own name is in three of these, which is exactly why they get
confused. They are unrelated.

| What you see | What it is | Where it lives |
|---|---|---|
| **Kiro Crew** | The product. | Everywhere |
| **Remote Crew** — spelled `instance` in config, routes and code (`instances.json`, `/api/instances`) | Another machine running its own gateway, reached over a tunnel. This page. | Settings → Remote Crew, the header switcher |
| **Crew Member**, or **crewmate** | A named assistant you keep: its own workspace, memory, agent template and model, with a standing thread. Not a machine. | The Crew Members page — see [crew-members.md](crew-members.md) |
| **Issue Radar crew** | An automation worker configured on a repository that claims an issue and moves it through the triage phases. Not a machine and not an assistant you chat with. | The Issue Radar Pipeline tab — see [issue-radar-pipeline.md](issue-radar-pipeline.md) |

The short test: a Remote Crew is a **host**, a crewmate is an **assistant**,
an Issue Radar crew is a **worker on a repo**.

## Tuning

Everything below is optional. The defaults are sized for a handful of machines
over a normal link.

| Setting | Default | What it does |
|---|---|---|
| `instances.enabled` | `false` | The opt-in. Read at startup, so changing it needs a restart. |
| `instances.warm_set_cap` | `0` | How many panes stay warm at once. `0` tracks how many machines you have registered, so up to an internal ceiling nothing is evicted. A number you set is honoured exactly. |
| `instances.tunnel_base_port` | `7778` | The first local loopback port handed out for a tunnel. |
| `instances.ssh_compression` | `true` | Compresses the tunnel. The whole remote dashboard travels over it, so this usually wins; turn it off on a fast local link. |
| `instances.connect_timeout_secs` | SSH 15s, SSM 25s | How long to wait for a tunnel to start accepting connections. Raise it for a host behind a jump host or `ProxyCommand`. |
| `instances.mint_timeout_secs` | SSH 30s, SSM 90s | How long to wait for the remote to mint a token. |
| `instances.max_recovery_attempts` | `8` | How many times a dropped tunnel retries itself before it gives up. |
| `instances.recover_backoff_max_secs` | `30.0` | Cap on the wait between those retries. |
| `instances.probe_failure_threshold` | `3` | How many failed health probes tear down a tunnel that has stopped forwarding. |

```bash
kirocrew config set instances.warm_set_cap 3
kirocrew config set instances.connect_timeout_secs 45
```

`instances.enabled` and `instances.tunnel_base_port` both need a restart: the
first is read once at startup, and the second only reaches tunnels built after
it changes, since the live ones already hold ports from the old base. The rest
apply to the running gateway.

## When something is wrong

Each row has a **Diagnose** button. It walks the path from the outside in and
reports the first broken link, which is usually the whole answer.

| What you see | What it means |
|---|---|
| `ssh_unreachable` / `ssm_unreachable` | The hub cannot reach the machine at all. Check the host alias and your key, or that the SSM agent is online. |
| `remote_down` | The machine is reachable but no gateway is listening on the port you configured. |
| `not_connected` | Everything is fine; this instance just has no tunnel yet. Click **Connect**. |
| `tunnel_down` | The forward died. Reconnect. |
| An SSH auth error on connect | Re-add your key to `ssh-agent`. Kiro Crew never prompts for a password, so a missing credential fails immediately — and the tunnel heals itself once the key is back. |
| A blank or black pane | The embedded dashboard never announced itself. Use **Retry** on the error panel. |
| A pane fails to load over an HTTPS tunnel with "this Remote Crew is too old for same-origin panes" | Over a published HTTPS origin the pane rides the same-origin relay, which needs the remote to advertise the hub's pane-relay protocol. Update the remote crew's Kiro Crew, then reconnect — see [Panes over an HTTPS tunnel](#panes-over-an-https-tunnel). |
| "local port N was taken while connecting" | Something grabbed the port first. Retry, or move `instances.tunnel_base_port` somewhere quieter. |
| A machine that keeps dropping | The tunnel already retried on its own for about two minutes before giving up, and then ran a diagnosis. Check the remote gateway and the link itself. |
| Every token mint fails on one machine, whose gateway is healthy | The remote's `kirocrew` on `PATH` probably points at an install that is not the one running. Reinstall it there. |

## Panes over an HTTPS tunnel

A remote pane is embedded as the remote dashboard on a hub-local loopback port
(`http://<hub-host>:<forward-port>`). That works when you reach the hub on
`localhost` — the desktop app, or an SSH-forwarded `http://localhost:<port>` —
because your browser can reach the same loopback the hub does.

When you reach the hub through a tunnel that publishes **one exact HTTPS host**
(for example `https://crew.example.ts.net`), that direct URL cannot be used: it
would be `http://<published-host>:<forward-port>`, which the browser blocks as
mixed content and which points at a hub-local port your browser can never reach.
So on a published HTTPS origin the hub embeds each pane through a **same-origin
capability relay** instead — panes work over the tunnel with no extra host, no
second port, and no mixed content. The switcher, the Remote Crew control plane,
and the chat proxy work over the tunnel unchanged too.

**How the relay works.** The hub serves each pane from a short-lived capability
path on its own published origin (`/instance-pane/<capability>/…`) inside a
sandboxed, **opaque-origin** iframe (the `sandbox` attribute omits
`allow-same-origin`, so the remote bytes served on the hub's origin get no access
to the hub DOM, cookies, storage, or session), and reverse-proxies that path —
HTTP **and** WebSocket — to the remote's loopback gateway over the SSH/SSM
forward the hub already holds. Because the pane is same-origin with the hub, the
parent's own `frame-src 'self'` admits it and no wildcard host or CSP widening is
needed.

The pieces:

1. **A relocatable dashboard runtime.** The SPA derives every request target from
   `window.location` (root-relative `/api`, `/assets`, the router basename, and
   `wss://…/api/ws`). `website/src/lib/dashboardRuntime.ts` reads a runtime base
   from the capability path so the same unmodified bundle keeps its traffic under
   the prefix. It resolves to the identity in direct mode, so localhost is
   byte-for-byte unchanged.
2. **The capability lease + relay route** (backend). Described below.
3. **The opaque-frame consumer.** The switcher requests a pane with
   `POST /api/instances/{id}/pane` (access `same-origin-relay`), renders the
   returned relay endpoint in an iframe with `allow-same-origin` omitted, and
   attributes remote→parent messages by an exact `contentWindow` + per-pane
   `channel` check — the opaque frame's `event.origin` is `"null"`, so it is
   never trusted. Downward delivery is the mirror image and is **document-bound,
   not broadcast**: every downward message (host model, readiness ack, cursor
   replies) rides an authenticated `MessageChannel` port the pane's own document
   transferred to the parent, never a `postMessage(msg, '*')` a replacement
   document could receive. A pre-module bootstrap the relay injects into the
   pane's entry HTML installs a bounded, parent-backed Web Storage adapter before
   any app module reads storage (an opaque origin's `localStorage` throws on
   access), and the parent persists each crew's pane storage under its own origin,
   namespaced per instance. The bootstrap GATES the entry module (served inerted)
   and — through **one protocol for every document**, the first load and every
   subsequent navigation or reload — derives its capability prefix from
   `location.pathname`, hands the parent one port of a fresh `MessageChannel` with
   the `documentPath` + a per-document nonce, and releases the module only once
   the parent's authenticated reply over that port delivers the channel + storage
   bank. The parent binds the request on the exact frame + issued `documentPath`
   (never the opaque origin); a first-load `window.name` envelope is only a
   synchronous storage accelerator (cleared on read), never the channel authority.
   A handshake that never authenticates leaves the module gated (fail closed) so
   the pane never boots with an empty channel and its storage reload loop — the
   parent's readiness watchdog surfaces Retry instead.

**Two things the relay requires of the remote.** The remote must advertise the
hub's pane-relay protocol version (`pane_relay_protocol` on `/api/status`); a
remote too old fails closed with an actionable **"this Remote Crew is too old for
same-origin panes; update it, then reconnect"** message (code
`remote_upgrade_required`) rather than a broken frame. And the forward must be up
when the pane is issued — the issuer connects the tunnel and mints the capability
in one call.

Direct-loopback (desktop app, `http://localhost:<port>`) is unchanged and does
NOT use the relay: it embeds the remote dashboard on the hub-local loopback port
exactly as before, validated by exact loopback origin.

### The capability relay (backend contract)

`POST /api/instances/{id}/pane` is the owner-only issuer
(`handlers_instances.api_instances_open_pane`). It returns **one complete
discriminated endpoint**:

- `direct-loopback` — the browser reaches the forward itself; the response
  carries the connected `local_port` and the remote `token`, exactly what
  `connect` hands back (confirmed through the shared token-confirm step).
- `same-origin-relay` — the response carries only a `documentPath`
  (`/instance-pane/<capability>/`), a `channel`, `protocol: 1`, and an expiry.
  **No port and no remote token cross this boundary.**

Before issuing a relay endpoint the hub reads the remote's advertised
`pane_relay_protocol` from its `/api/status`. A remote that does not advertise
this hub's protocol (too old, or unreachable) **fails closed** with
`409 remote_upgrade_required` — the hub never falls back to HTTP mixed content, a
raw loopback port, or a sandbox-less frame.

The lease (`dashboard/instance_pane_relay.py`) is **memory-only** and stores only
digests of the capability and channel. It is bound to the instance id, the live
tunnel/forward **generation**, an expiry, and the channel digest, and is bounded
per instance and globally. It is revoked on disconnect, rebuild, removal, manager
close, tunnel replacement, and expiry — enforced fail-closed at serve time by a
generation check, so a lease is dead the moment its forward moves whether or not
any explicit cleanup runs.

The self-authenticating route `* /instance-pane/{capability}/{tail}` sits before
token auth and the SPA shell (its prefix is on `token_auth`'s bypass list; the
handler is the gate). A missing, malformed, expired, unknown, or stale-generation
capability all answer one **uniform 404** before the peer, the request body, or
any error detail is touched. For a live lease the relay forwards HTTP and
WebSocket **only through the current manager-owned peer**, using the manager's
credential/session machinery — never an arbitrary host, port, or URL. It:

- strips the browser's cookies, `Authorization`, `Origin`, `Referer`, `Host`, and
  every forwarding header; the manager adds its own port-scoped session cookie,
  so no browser identity crosses the tunnel;
- strips the peer's `Set-Cookie` and `Clear-Site-Data` downstream;
- preserves the method, exact path and query, range/conditional behavior, status,
  content type, and streaming (including SSE), within bounded request/response/
  idle/frame sizes;
- rewrites a same-peer root redirect under the capability prefix and **fails
  closed** on a cross-authority or protocol-relative redirect; the WebSocket
  handshake likewise refuses to follow any peer redirect (a trace hook raises
  before a second request is sent, since `ws_connect` has no `allow_redirects`);
- relocates only the entry HTML's root markers (re-rooting the root-absolute
  `src`/`href` entry markers under the capability prefix, with **no `<base>`
  element** — a `<base>` would retarget in-document SVG `url(#id)` fragments and
  `#hash` anchors; relative chunk/asset refs self-locate) — it never rewrites
  built JavaScript;
- stamps every response (except script types) with a `sandbox` CSP, `nosniff`,
  `no-referrer`, and `Access-Control-Allow-Origin: null` (no credentials), so the
  frame is opaque even if a relay URL is opened as a full tab;
- keeps the capability out of both owned log sinks: the relay's SEL audit records
  only a fixed reason (never the capability, channel, token, cookie, full path,
  query, or body), and a route-aware redactor masks the capability in aiohttp's
  access log at the server boundary.

## Next

- [Setting up Remote Crew on an EC2 instance](../../../docs/guides/remote-crew-on-ec2.md) — both transports end to end, plus the EC2 gotchas.
- [Native SSM vs legacy SSH](../../../docs/guides/cloud-instance-ssm-vs-ssh.md) — how a cloud-launched machine is reached.
- [Remote and mobile access](../../../docs/guides/remote-and-mobile.md) — installing and running a gateway on the far machine in the first place.
- [dashboard.md](dashboard.md) — the dashboard the switcher sits in.
- [crew-members.md](crew-members.md) — crewmates, which are not machines.
