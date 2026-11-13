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
holds. The [EC2 guide](https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/remote-crew-on-ec2.md) walks both
ends of it, and
[SSM vs SSH](https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/cloud-instance-ssm-vs-ssh.md) covers why a
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

**From a link (desktop app).** A page that lists your machines can offer a
`kirocrew://remote-crew/add?host=<ssh host>&user=<user>&port=<n>&name=<label>`
link. Opening it brings the desktop app forward on this panel with the add form
filled in and a line saying so. Only `host` is required; `user` becomes
`user@host`, and `port` is the remote gateway's port (the Remote port field),
not the port ssh dials. Nothing is saved and nothing connects until you press
**Add remote crew**, so first check that the SSH host is a machine you use:
after that click the crew is like one you typed in, and with **Auto-connect crews** on (the default) it
connects as soon as it is added. A link with any other key, a host the SSH host
field would refuse, or a malformed port or label opens nothing.

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

## Turning one crew off

Each crew in **Settings → Remote Crew** has a switch beside its status that reads
**Connections: On** or **Connections: Off**. Off closes that crew's tunnel and
keeps it closed: no click, auto-connect or gateway restart opens it again. Its
sidebar group reads **Off** with an **Enable** button, and an open pane for it
shows offline. A crew reached through it stays closed too, and connecting one
names the crew that is off. Turning it back on reconnects it. The crew's
settings are kept the whole time. Only the owner can flip the switch.

## Crews reached through another crew

A remote pane is a full dashboard, so it can list remote crews of its own. When
you connect one of those inside the pane, your dashboard adopts it as a
top-level entry in your own switcher. You do not add it twice. The connection
rides the crew that reaches it; your dashboard never dials that machine itself.

A chain is at most two hops deep (you → B → C), and one crew can carry at most
eight crews behind it. In Settings → Remote Crew such a row shows its host as
**`<host>` (reported)**: it is the address that crew reports, not one you can
reach. When a crew in the middle of a chain is down, the entries behind it read
**unreachable — `<name>` is down**, naming the hop to fix.

Removing a crew also removes every crew reached through it.

## A session that runs on another machine

**New chat on crew** creates a session ON a connected machine and opens it here in
a crew window. It is a feature preview: turn on **Settings → Developer → Feature
Previews → Chat on a crew**, then pick it from the new-chat menu and choose one of
your connected machines. Only connected ones are offered, and creating one fails
outright when the machine runs a Kiro Crew version too far from yours.

The window shows the remote's own transcript, running state and pending approval.
Send, stop, approve, continue, regenerate and rewind call the remote's own routes,
so its prompts, tools and model all run over there, against its agents and
workspace. Nothing is stored on this machine, and every reply is redacted before
it renders.

Two things follow from the split:

- **The remote's roster wins.** The session uses the remote machine's agents,
  models and workspaces, not this machine's. Your local default agent is not sent
  over; a name from your roster would mean nothing, or something else, over
  there. Your default memory mode is sent, so a new session does not quietly run
  in a different one.
- **The remote owns the turn.** A turn already in flight keeps running on the
  remote if you close the window or your browser goes away.

A chat an older version ran on a crew through the retired turn relay stays in your
list as a **read-only archive**. You can read it, but every action that would run
it answers `relay_archive_read_only`; open the crew's own session to keep going.

## Another machine's sessions in your list

The opposite direction: sessions a connected machine **owns** are listed in
this dashboard's Sessions list. The same switch holds both: **Settings →
Developer → Feature Previews → Chat on a crew**. With it off, nothing is fetched
at all.

With it on, the list is grouped per machine: **Local** first, then one
collapsible group per crew, its header badged **online**, **reconnecting**,
**error** or **offline** from the tunnel's state. A crew's group holds the
sessions it owns and any read-only relay archive that ran on it. Inside the group
the crew's own folders nest its rows, with unfiled rows last — a read-only
mirror of the crew's folder tree, edited on the crew, each space keeping its own
collapse state. A crew that
disconnects keeps its last listed rows, dimmed and without live status, until
you reload; nothing asks it again while it is down. Collapsing a group never
changes which session is open. With no crew to group, the list has no group
headers and looks as it always did. The board view groups nothing: it leaves
peer rows out and says how many it left out.

Clicking a crew's session opens it in a crew window, the same window **New
chat on crew** opens: the remote machine's own transcript, running state and any
pending approval, driven over the connection. Nothing is copied to this machine.

A peer row deliberately does less than a local one, because the other machine —
not this one — owns the session:

| Not on a peer row | Why |
|---|---|
| Rename, close, pin | They change the session, which lives on the other machine. |
| Folders and drag | Filing is local bookkeeping about local rows. |
| The `⋯` group actions | Same reason: they act on sessions this machine owns. |
| The board view | It stays local, and reports how many rows it filtered out. |

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

## Opening a remote Crew through the Kiro relay

Everything above reaches a machine over a tunnel you set up. The desktop app has
a second way in that needs no tunnel and no SSH key: it asks the control plane
which remote Crews your Kiro sign-in may open, and opens one through the Kiro
relay. This is the viewer side only. A Crew you open this way still needs Kiro
Relay turned on where it runs.

It is off by default, and only the desktop app offers it. A browser tab or the
installed web app shows nothing new.

### Turning it on

Two settings, both on the gateway the desktop app runs:

```bash
kirocrew config set tunnel.relay_viewer_enabled true
kirocrew config set tunnel.relay_viewer_cps_url https://<control plane host>
```

`kirocrew config set` saves through the gateway's own writer, and the gateway reads
both settings on every request, so the change takes effect at the next request with
no restart. The settings page can't change them: the dashboard's config route
refuses `tunnel.relay_viewer_enabled` as not editable.

A hand edit of `config.json` works differently. On a Linux host where the agent
sandbox seals that file, the gateway keeps serving its own last-saved copy and holds
the edit as a pending change, even across a restart, until you apply it in
**Settings → Security**. Until then it has no effect.

Reload the dashboard once the change is in effect: a tab that last saw the feature
off stops asking until it reloads. The control plane URL must be `https://`
(`http://` only for a loopback port-forward). A URL that breaks that rule is
ignored with a warning, and the list then says Team Crews over the relay aren't
set up.

The list is fetched with your Kiro sign-in, read from the gateway's own sign-in
store. If you are signed out, it says to sign in to Kiro. An IAM Identity Center
account that has no Kiro profile can't sign in today: the sign-in stops at the
profile step and nothing is saved, so the list stays signed out.

### The Team Crews group

With the feature on, the Crew switcher in the top header gains a **Team Crews**
group. It lists the Crews the control plane lists for you, each with its name,
**Personal** or **Team**, and its status. A Crew is **online**, and choosing its
row opens it, only while the relay reports its tunnel connected. A Crew whose host
still renews its lease but whose tunnel is down shows **reconnecting**, any other
shows **offline**, and either may also show when it was last seen. A row that
isn't online is dimmed, and choosing it does nothing. If the list can't be loaded,
the group says why, with the request ID as plain text when there is one. The list
is read again about once a minute while the dashboard is open, and it never
carries an access token. The group appears only once your account has at least
one Team Crew, or when it has a sign-in or load problem to show.

### What opening a Crew does

Choosing an online row sends only the Crew's endpoint ID to the desktop app's
main process. Main asks this machine's gateway for a short-lived access token,
presenting the gateway's local secret, so a web page can't make that call. The
gateway lists your Crews again, refuses one that is no longer in your list, and
only then asks the control plane for the token. If any step refuses, no window
opens, the switcher stays open, and the row shows the reason in plain words.

The Crew then opens in its own window, with its own session that is kept in
memory and cleared when the window closes:

- Main adds the access token, in the `Kiro-Relay-Authorization` header, to every
  request for that Crew's own address, WebSocket connections included, and to
  nothing else. A copy of the header set by the page is dropped.
- The page gets no preload script, so its JavaScript can't read the token.
- Permission prompts (camera, notifications and the like) are refused.
- A link to another site opens in your browser. A pop-up window on the Crew's own
  address is refused.
- One window per Crew. Opening a Crew from its row when it is already open brings
  its window forward.

### Keeping the token fresh

Main renews the access token before it runs out. When the token's answer names a
renewal time, main uses it, with a few seconds of random spread so open windows
don't renew in step.
While a window is minimized or hidden it renews once and then pauses until you
show it again.

- If the Crew's page answers 401, main gets a new token once and reloads. A second
  401 soon after stops renewal for that window.
- If access is refused (no longer granted, remote access off, the Crew offline,
  or the feature turned off), renewal stops. The window stays open but
  sends no token from then on. Opening that Crew from its row again tries a fresh
  token and reloads.
- If your Kiro sign-in has ended, main stops every renewal and closes every relay
  Crew window when a renewal or opening a Crew from its row gets the signed-out
  answer. Main doesn't watch for a sign-out between renewals, so windows can stay
  open until then.
- If the control plane can't be reached or answers with an error, main retries
  with a growing wait. When it asks to slow down, main waits as asked once.

Renewal keeps new requests signed in, but it doesn't extend a WebSocket that is
already open. The relay closes an open socket with code 4401 about 30 seconds after
the token it opened with runs out, whatever renewals landed in the meantime. The
Crew's page then reconnects, and main adds the newest token it holds to the new
connection.

Main can't see the relay closing a WebSocket, so a Crew whose remote access was
turned off is noticed at the next renewal.

### What stays local

Both gateway routes behind this (`GET /api/relay-viewer/crews` and
`POST /api/relay-viewer/token`) answer 404 while the feature is off, and the relay
refuses them, so nobody can reach them through it. Only the owner's dashboard reads
the list. Only the desktop app's own main process can get a token, from this
machine. No token, sign-in or credential is logged.

`tunnel.relay_viewer_credential` is for testing only. It names where a test
credential is kept, as `secret://NAME` or the absolute path of a file, and the
viewer then uses that credential instead of your Kiro sign-in. Leave it empty.

## The four things called "crew"

The product's own name is in three of these, which is exactly why they get
confused. They are unrelated.

| What you see | What it is | Where it lives |
|---|---|---|
| **Kiro Crew** | The product. | Everywhere |
| **Remote Crew** — spelled `instance` in config, routes and code (`instances.json`, `/api/instances`) | Another machine running its own gateway, reached over a tunnel. This page. | Settings → Remote Crew, the header switcher |
| **Bot** (earlier called Crew Member or mate) | An always-on AI agent with its own workspace, memory, agent template and model, with a standing thread. Not a machine. | The Bots page — see [crew-members.md](crew-members.md) |
| **Issue Radar crew** | An automation worker configured on a repository that claims an issue and moves it through the triage phases. Not a machine and not an assistant you chat with. | The Issue Radar Pipeline tab — see [issue-radar-pipeline.md](issue-radar-pipeline.md) |

The short test: a Remote Crew is a **host**, a Bot is an **assistant**,
an Issue Radar crew is a **worker on a repo**.

## Tuning

Everything below is optional. The defaults are sized for a handful of machines
over a normal link.

| Setting | Default | What it does |
|---|---|---|
| `instances.enabled` | `false` | The opt-in. Read at startup, so changing it needs a restart. |
| `instances.warm_set_cap` | `0` | How many panes stay warm at once. `0` tracks how many machines you have registered, so up to an internal ceiling nothing is evicted. A number you set is honoured exactly. |
| `instances.default_destination` | `""` | The crew **Send a copy to** lists first, marked as the default. Empty means no default. A folder's own **Default destination crew** (Folder settings) overrides it for sessions in that folder. Nothing is sent until you click. |
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
| `ssm_target_missing` | SSM lists no managed node with this crew's `ssm_target`. If the instance was terminated or replaced (an Auto Scaling instance refresh gives it a new id), edit the crew and set `ssm_target` to the new id. Otherwise check the AWS profile and region, and that the instance has registered with SSM (agent running, instance profile granting `AmazonSSMManagedInstanceCore`). |
| `ssm_access_denied` | Your AWS profile was refused `ssm:SendCommand` on the instance. Add the three SSM permissions under [SSH or SSM](#ssh-or-ssm-which-transport) to the profile's IAM policy. |
| `ssm_user_unknown` | The **Remote user** does not exist on the instance (Ubuntu images use `ubuntu`, not `ec2-user`). Set **Remote user** in **Edit settings**. |
| `remote_wrong_port` | A Kiro Crew gateway answers on the instance, on another port than the one you configured. Set **Remote port** to the port the reason names. |
| `remote_not_installed` | No `kirocrew` launcher for the **Remote user** where the token mint looks for one, and no sign a gateway ever ran there. Install Kiro Crew there as that user, or set **Remote kirocrew path**. |
| `remote_down` | The machine is reachable but no gateway is listening on the port you configured. |
| `not_connected` | Everything is fine; this instance just has no tunnel yet. Click **Connect**. |
| `tunnel_down` | The forward died. Reconnect. |
| An SSH auth error on connect | Re-add your key to `ssh-agent`. Kiro Crew never prompts for a password, so a missing credential fails immediately — and the tunnel heals itself once the key is back. |
| A blank or black pane | The embedded dashboard never announced itself. Use **Retry** on the error panel. |
| "local port N was taken while connecting" | Something grabbed the port first. Retry, or move `instances.tunnel_base_port` somewhere quieter. |
| A machine that keeps dropping | The tunnel already healed itself before giving up and running a diagnosis: about two minutes when the tunnel cannot be rebuilt, or roughly attempts × (probe window + backoff), about 6 minutes at the default 8 `instances.max_recovery_attempts`, when the forward re-binds but the far end stays dead. Check the remote gateway and the link itself. |
| "the SSM session … was closed by AWS after a period with no activity" (or "AWS ended the SSM session") | AWS closed the SSM or Fargate forward. Open a new one with **Connect** on the crew's card in Settings. To allow longer idle periods, raise the Session Manager idle-timeout preference for that account and region. |
| **unreachable — `<name>` is down** on a chained crew | The crew it is reached through is down. Fix that crew's row first; the chained one comes back with it. |
| Every token mint fails on one machine, whose gateway is healthy | The remote's `kirocrew` on `PATH` probably points at an install that is not the one running. Reinstall it there. |

## Next

- [Setting up Remote Crew on an EC2 instance](https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/remote-crew-on-ec2.md) — both transports end to end, plus the EC2 gotchas.
- [Native SSM vs legacy SSH](https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/cloud-instance-ssm-vs-ssh.md) — how a cloud-launched machine is reached.
- [Remote and mobile access](https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/remote-and-mobile.md) — installing and running a gateway on the far machine in the first place.
- [dashboard.md](dashboard.md) — the dashboard the switcher sits in.
- [crew-members.md](crew-members.md) — Bots, which are not machines.
