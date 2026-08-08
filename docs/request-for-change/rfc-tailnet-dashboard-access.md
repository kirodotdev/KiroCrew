---
title: Tailnet-native dashboard access
status: partial
author: zezhexu
created: 2026-08-06
last-audited: 2026-08-06
audited-at: 429cbad8
doc-pr: 1748
implementation-prs: [1761]
tracking-issues: [1762]
supersedes: []
superseded-by: []
---
# RFC: Tailnet-native dashboard access

- Status: partial — **Phase 1 landed** as PR #1761 (merged commit `f8afcff7`):
  `is_proxied_request()`, the per-binding `proxied` flag, the tri-state Security
  Posture row, and the guide correction across all three tunnel providers.
  Phases 2–4 have no implementation. Phase 1 reports the pin's real scope; it
  does **not** repair the pin, which is Phase 3 and is tracked as issue #1762.
  Pin scope was settled after review: configurable, defaulting to `node` (§3.1).
- Author: zezhexu
- Created: 2026-08-06
- Related: `docs/guides/remote-and-mobile.md` (the guide this RFC corrects and
  extends), `docs/request-for-change/rfc-update-architecture.md` (same
  "the backend decides, the SPA renders" organizing rule)

## Summary

Kiro Crew's remote-access story is "bind loopback, mount token auth, put a
tunnel in front". `tailscale serve` fits that shape exactly and works today with
no code changes. But it works *by accident*, and two of the properties the
documentation advertises as mitigations do not hold behind it.

The larger of the two is not Tailscale-specific. **Token IP pinning is inert
behind every tunnel the guide recommends.** The pin is taken from
`request.remote` (`dashboard/token_auth.py:1506`), and every recommended tunnel
— cloudflared, ngrok, Tailscale — runs on the gateway host and connects from
loopback. So the token binds to the proxy, not to the user, and the guide's
security note offers that pin as a mitigation for the public exposure it just
warned about (`docs/guides/remote-and-mobile.md:283`). The same substitution
makes the SEL audit trail record `127.0.0.1` as the caller for every remote
request (ten sites, `token_auth.py:1266`–`:1416`).

Tailscale is the one provider that can *repair* this rather than just document
it, because `tailscale whois` resolves the real peer from the local daemon. This
RFC therefore treats identity-pinned sessions as a defect fix, not a
convenience feature, and makes `tailscale serve` a first-class documented path
with the manual origin configuration removed.

Explicitly out of scope: Tailscale Funnel, a generic trusted-reverse-proxy
setting, and any change to which endpoints refuse forwarded requests.

## Motivation

### Current state

Remote access has exactly one shape. `is_local_only()` (`dashboard/urls.py:166`)
always returns `True` in the OSS build — its only widening branch depends on
`devspaces_proxy_url()` (`:150`), which always returns `None` — so the dashboard
binds loopback unconditionally. `KIROCREW_BIND` (`:186`) overrides the bind
address only, and is documented as existing for containers.

Everything else is layered on top of that loopback socket:

| Layer | Where | Behind a same-host proxy |
|---|---|---|
| Token auth (always mounted) | `token_auth.py` | works |
| CSRF origin allowlist | `urls.py:382` `build_allowed_origins` | needs `dashboard.url` |
| DNS-rebinding `Host` barrier | `origin.py:198` `check_host` | needs `dashboard.url` |
| Token IP pin | `token_auth.py:1506`–`:1508` | **inert** |
| Audit caller identity | `token_auth.py:1266`–`:1416` | **always `127.0.0.1`** |
| Config-write / secret-reveal refusal | `origin.py:86` | works (fails closed) |

The last row works well and is not changed by this RFC.
`is_direct_local_request()` requires a loopback peer **and** the absence of every
header in `_PROXY_FORWARD_HEADERS` (`origin.py:77`), so a proxied request is
correctly treated as remote and the config-write surfaces return `read_only`
(`handlers/messaging.py:2355`, `:2633`, `:2894`).

### Problem 1 — the IP pin is inert behind every recommended tunnel

`check_token_ip()` (`token_auth.py:928`) compares against the value captured at
`:1506`, which is `request.remote`. For cloudflared, ngrok, and
`tailscale serve` alike the immediate peer is `127.0.0.1`, so:

- the token binds to the proxy on first use and then matches every subsequent
  request from anyone arriving through the same proxy;
- a leaked link is a bearer credential for its full session lifetime — up to the
  20-hour `MAX_SESSION_TTL_SECS` ceiling, and `kirocrew token` defaults straight
  to `20h`;
- there is no record of who used it, because the audit `caller` is the proxy.

This is a pre-existing gap, not one Tailscale introduces. It is stated here
because it sets the bar: any RFC that makes remote access *easier* without
addressing it makes the gap more reachable.

### Problem 2 — the tailnet mode that fits is the one not documented

`docs/guides/remote-and-mobile.md:263` lists **Tailscale Funnel** alongside
cloudflared and ngrok, under a heading whose warning reads "A tunnel puts your
dashboard on the public internet." That warning is correct for Funnel: Funnel is
the public-ingress mode.

`tailscale serve` — the tailnet-only mode — is not mentioned. It is strictly
better than every option in that section for the phone case that section exists
to solve:

- reachable only from inside the tailnet; nothing is published publicly;
- TLS with a real certificate, so no self-signed prompt and `wss://` works;
- a stable MagicDNS hostname, satisfying the guide's own "use a named tunnel"
  requirement without an account with a tunnel provider;
- attaches `X-Forwarded-For` and `X-Forwarded-Proto`, so `is_https_request()`
  (`origin.py:114`) sets a `Secure` cookie and `is_direct_local_request()` keeps
  the config-write surfaces closed;
- carries `Tailscale-User-Login`, the only per-request identity signal any
  supported path provides.

So the guide steers users toward the public mode and omits the private one.

Two of those properties are **upstream behaviours, not contracts**:
`X-Forwarded-Proto` was added to Serve only after tailscale/tailscale#7061, and
the identity headers after tailscale/tailscale#6954. Both are present in current
Tailscale, but neither is a documented stability guarantee, so neither may be
load-bearing. §2 is designed accordingly: the daemon is the source of identity,
so an absent login header costs nothing (only a *disagreeing* one is a
rejection), and an absent `X-Forwarded-Proto` costs the `Secure` cookie
attribute, not access. What *is* load-bearing is `X-Forwarded-For`, without
which no peer resolves and everything falls back to token auth. A minimum
Tailscale version should be recorded when Phase 3 lands.

### Problem 3 — the origin allowlist is a manual step that fails silently

With Serve running, opening the MagicDNS URL without first setting
`dashboard.url` returns `403` from `check_host()`. The response does not say
which allowlist rejected it or how to extend it. `build_allowed_origins()`
(`urls.py:382`) already has the right shape for a fix — the
`devspaces_proxy_url()` hook is precisely an "origin supplied by the
environment" slot — but no environment supplies one in OSS.

## Goals

1. `tailscale serve` is a documented, first-class remote-access path, reachable
   in one command with no config editing.
2. A session reached over the tailnet is pinned to a **verified** peer identity
   instead of to the proxy's loopback address, and the audit trail records that
   identity.
3. Enabling identity trust cannot silently widen access to an entire shared
   tailnet.
4. Every failure mode falls back to the existing token path. Nothing this RFC
   adds can lock a user out or grant access on ambiguity.
5. The set of endpoints that refuse forwarded requests is unchanged.

## Non-goals

- **Funnel.** Public ingress is a different risk class. `whois` returns nothing
  for a public source address, so Funnel degrades to the token path
  automatically; the only change owed to Funnel is a documentation correction.
- **A generic trusted-reverse-proxy setting.** A `trusted_proxies` + XFF-depth
  configuration is the classic footgun of this problem space and can only relay
  an identity claim, never verify one. See Alternatives.
- **Tailscale as an Instances transport.** SSH over a tailnet is ordinary SSH;
  the Instances feature already works over a MagicDNS host with zero code.
- **Widening `is_direct_local_request()`.** A verified tailnet identity is not
  the local machine.
- **Replacing token auth.** Identity trust is an additional, opt-in pin — the
  token path remains the only path when identity cannot be established.

## Design

### §1 The organizing rule

> The immediate peer decides whether a forwarded header may be read at all. The
> local daemon, not the header, decides who the peer is. The header is only
> corroboration.

The first clause already exists: `is_https_request()` honours
`X-Forwarded-Proto` **only** when the immediate peer is loopback, on the
reasoning that a remote attacker cannot reach the socket to forge it. This RFC
reuses that rule verbatim and adds the second and third clauses.

### §2 Forwarded-peer resolution

A new module `src/kiro_crew/dashboard/tailnet.py` exposes one function:

```python
def resolve_forwarded_peer(request: web.Request) -> ForwardedPeer | None
```

It returns a peer only when **all** of the following hold, and `None` otherwise:

1. `is_loopback(request.remote)` — the immediate peer is the local proxy.
2. Tailnet trust is enabled (§4).
3. `X-Forwarded-For` carries **exactly one** address. Two or more means a
   proxy chain we cannot attribute; reject rather than take the first or last.
4. That address parses and falls inside the tailnet ranges
   (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`).
5. The local daemon resolves it: `tailscale whois --json <addr>` returns a node
   in this tailnet.
6. The resolved login equals the `Tailscale-User-Login` header, when present.
   A mismatch is a rejection, not a warning.

`ForwardedPeer` carries `login`, `node`, and the raw address. Nothing else in
the codebase may read `Tailscale-User-Login` directly.

Operational constraints, all fail-closed:

- **Timeout.** A hard timeout on the daemon call; expiry returns `None`.
- **Caching.** Results cached by address with a short TTL and a bounded entry
  count, so a request storm cannot fork a daemon call per request. The
  WebSocket path resolves once at upgrade.
- **Daemon absent or down.** `None`, so the request falls through to token auth.
  A stopped `tailscaled` degrades access, it does not deny it.

### §3 Identity-pinned sessions and the login allowlist

Session pinning generalises from an address to a **peer key**:

- today, and unchanged when no peer resolves: `ip:<request.remote>`
- when a peer resolves: `ts:<login>@<node>` (default) or `ts:<login>`, per §3.1

`check_token_ip` / `bind_token_ip` become `check_token_peer` /
`bind_token_peer` over that key. The IP branch is byte-for-byte the current
behaviour; only the tailnet branch is new. A session pinned to a node cannot be
replayed from another node even inside the same tailnet — strictly stronger than
today's Serve behaviour, where every peer shares one pin.

The audit sites take the resolved login as `caller` when a peer resolved, so the
remote-access audit trail names a person instead of the proxy.

**A login allowlist is mandatory, not optional.** A work tailnet can have
hundreds of members, and "any node in the tailnet" would hand them the
dashboard. So:

```json
{
  "dashboard": {
    "tailscale": {
      "enabled": true,
      "trust_identity": true,
      "allowed_logins": ["you@example.com"],
      "pin_scope": "node"
    }
  }
}
```

`trust_identity: true` with an empty `allowed_logins` is a **configuration
error**: it is refused at load with a logged reason and identity trust stays
off. This must not be a silently-permissive default. `KIROCREW_OWNER_ID` cannot
supply the default — it is a chat-platform user id, a different namespace from a
Tailscale login.

### §3.1 Pin scope is configurable; the default is `node`

`pin_scope` selects what a resolved session is pinned to. It is **not** the
control that decides who may connect — that is `allowed_logins`. `pin_scope`
only decides what happens *after* a session cookie leaks.

| Value | Peer key | A leaked cookie is usable from | Costs |
|---|---|---|---|
| `node` (**default**) | `ts:<login>@<node>` | only the original device | a device whose node identity changes (Tailscale reinstall, `logout` + re-login) loses its session |
| `login` | `ts:<login>` | any device carrying that Tailscale identity | lateral movement across one user's own devices is free |

**`node` is the default** because the flow it costs does not work today either:
the current IP pin already refuses a link opened on a laptop and then forwarded
to a phone. Defaulting to `node` therefore takes no working behaviour away,
while `login` would be a deliberate relaxation. `login` exists for operators who
would rather re-issue nothing when a device is re-enrolled.

**A tagged node is always pinned to `node`, whatever `pin_scope` says.** This is
a hard override, not a preference. An ACL tag *replaces* the user identity on a
device, so `tailscale whois` reports the login of every tagged device as the
literal `tagged-devices` (tailscale/tailscale#4605). Under `pin_scope: "login"`
that single value would be the peer key for the **entire tagged fleet** — one
leaked CI-runner session would be replayable from any other tagged node, and an
`allowed_logins` entry of `tagged-devices` would admit all of them. Node scope
is unaffected, because node identity stays unique for tagged devices. So: when
the resolved login is `tagged-devices`, force node scope and log that the
configured scope was overridden.

An unrecognised `pin_scope` value falls back to `node` with a logged warning —
the same direction as `KIROCREW_BIND`'s invalid-value handling, where a typo can
only ever narrow exposure, never widen it.

`pin_scope` is nested under `tailscale` because a resolved peer identity is
Tailscale-specific today. If a second identity-bearing proxy is ever added
(§2, "Alternatives"), the key moves up a level; that rename is cheap while there
is one provider and no persisted format depends on it.

### §4 Origin auto-derivation

`build_allowed_origins()` gains a tailnet contribution beside the existing
`devspaces_proxy_url()` hook: read `Self.DNSName` from `tailscale status --json`,
strip the trailing dot, add `https://<dnsname>`. `build_allowed_hosts()` derives
the `Host` allowlist from that same set, so `check_host()` follows with no
second change — the existing single-source-of-truth property is preserved.

This removes the manual `dashboard.url` step and Problem 3's silent `403`.

Two opt-in signals, in order:

1. `dashboard.tailscale.enabled` — explicit, wins if set.
2. Otherwise, `tailscale serve status` reporting the dashboard port as a Serve
   target. An operator who has told `tailscaled` to serve our port has already
   expressed the intent; inferring it from that is a read of configuration, not
   a guess.

Signal 2 covers auto-origin only. `trust_identity` is always explicit.

### §5 What stays closed

No change to `origin.py:86`. A verified tailnet peer still sends
`X-Forwarded-For`, so `is_direct_local_request()` still returns `False` and the
secret-reveal and channel-config surfaces stay read-only. This is intentional
and gets a regression test: *a whois-verified tailnet peer receives
`read_only: true`* on the `handlers/messaging.py` surfaces.

From a phone over the tailnet you can chat, read sessions, approve tools, and
manage cron. You cannot reveal a stored credential or rewrite channel config.

### §6 Link unfurl (deferred, and may be refused)

`link_unfurl.py:193` rejects non-`is_global` addresses, which covers
`100.64.0.0/10`, so tailnet URLs pasted into chat do not unfurl.
`test/test_link_unfurl.py:168` pins that rejection against a table of IANA
special-purpose prefixes.

Allowing it is a **deliberate widening of the agent's egress surface** — a chat
message would be able to make the gateway `GET` an internal service — and is
therefore split into its own phase rather than bundled as a small fix. If
pursued: keep the default denial and the test table intact, and permit only an
address the daemon confirms belongs to this tailnet. This phase may be rejected
outright without affecting the rest.

## Phases

Each phase is independently shippable and independently abandonable.

| Phase | Content | Touches auth? | Touches egress? | Blocked on |
|---|---|---|---|---|
| 1 | Docs correction, posture visibility | no | no | — |
| 2 | Origin auto-derivation (§4) | no | no | OQ1 (for the inferred signal only) |
| 3 | Forwarded-peer resolution + identity pinning + allowlist (§2, §3) | **yes** | no | OQ4 |
| 4 | Tailnet unfurl (§6) | no | **yes** | OQ3 — may be refused outright |

### Phase 1 — Documentation and visibility

Corrects the guide and makes the current state observable. Worth landing on its
own even if nothing else here does: today a user cannot see that their session
pin is inert.

The correction is **not scoped to the Tailscale rows**. Problem 1 is equally
false for cloudflared and ngrok — both also run on the gateway host and connect
from loopback — so fixing only the Tailscale rows would knowingly leave the guide
wrong for the other two at no saving.

Exit criteria:

- `docs/guides/remote-and-mobile.md` documents `tailscale serve` as a distinct
  path from Funnel, and no longer implies Funnel keeps the service private.
- Its security note no longer offers the IP pin as a mitigation for **any**
  same-host-proxied path — cloudflared, ngrok and Tailscale alike — and says
  plainly what the pin does and does not bind to behind a local proxy.
- Security Posture reports what the session pin is bound to, via a
  `_token_auth_items()` entry (`security_posture.py:891`), distinguishing
  "pinned to peer address" from "pinned to a local proxy".
- A test asserts that entry reports the proxy-pinned state when the request
  carries `X-Forwarded-For` from a loopback peer.
- No change to any auth or egress code path.

### Phase 2 — Origin auto-derivation

Exit criteria:

- With `tailscaled` running and Serve configured for the dashboard port,
  `build_allowed_origins()` contains `https://<Self.DNSName>` with no
  `dashboard.url` set.
- Opening that URL returns the SPA, not `403` from `check_host()`.
- `build_allowed_hosts()` is not modified: the `Host` allowlist follows from the
  origin set, preserving the existing single-source-of-truth property.
- With `tailscaled` absent, the origin set is byte-for-byte what it is today.
- A test asserts the inferred signal (§4 signal 2) does not fire when the Serve
  target is a port other than the dashboard's.

Entry depends on OQ1 only for the inferred opt-in; the explicit
`dashboard.tailscale.enabled` flag can ship without it.

**Ordering dependency on Phase 3.** This phase increases how many users reach
the inert-pin condition of Problem 1, so it may not ship silently. **The default
is: Phase 2 ships with the guide warning from Phase 1 already in place, and Phase
3 tracked as owed work.** Landing the two together is better and is preferred
when Phase 3 is ready — but it is not a precondition. Phase 3 needs adversarial
review and may stall on OQ4, and an origin fix held hostage to an auth
redesign tends to ship as neither. The warning is the mechanism that makes
shipping Phase 2 alone honest rather than silent.

### Phase 3 — Identity-pinned sessions

The only phase that touches the auth path. Requires adversarial review.

Exit criteria:

- `resolve_forwarded_peer()` returns `None`, and the request falls through to
  existing token auth, for every one of: non-loopback immediate peer; trust
  disabled; zero, two, or more `X-Forwarded-For` values; an address outside the
  tailnet ranges; `whois` failure, timeout, or empty result; a
  `Tailscale-User-Login` header disagreeing with the resolved login.
- A session bound to `ts:<login>@<node>` is rejected when replayed from a
  different node in the same tailnet.
- Under `pin_scope: "login"` that same replay is **accepted**, and a replay
  carrying a different login is rejected — the two scopes are separately pinned,
  so neither can silently become the other.
- A **tagged** node is pinned to `ts:<login>@<node>` even with
  `pin_scope: "login"` configured, and the override is logged. Asserted against a
  `whois` result whose login is `tagged-devices`.
- An unrecognised `pin_scope` value falls back to `node` with a logged warning,
  never to `login`.
- A node-scope rejection carries a reason naming device identity, distinct from
  the address-mismatch reason, so a re-enrolled device does not surface as an
  unexplained `IP mismatch`.
- `trust_identity: true` with empty `allowed_logins` is refused at config load
  with a logged reason, and identity trust remains off.
- A login outside `allowed_logins` is rejected even when `whois` resolves it.
- Audit records name the resolved login as `caller` when a peer resolved, and
  `request.remote` otherwise.
- A whois-verified tailnet peer still receives `read_only: true` from the
  `handlers/messaging.py` config surfaces — `is_direct_local_request()` is
  unchanged (§5).
- Resolution is cached per address with a bounded entry count, and the WebSocket
  path resolves once at upgrade rather than per frame.
- Every gate green with `tailscaled` absent from the CI host.

Entry depends on OQ4 (Windows behaviour) — pin scope is settled (§3.1). If OQ4
resolves negatively, this phase is POSIX-only and degrades to token auth
elsewhere.

### Phase 4 — Tailnet unfurl

Blocked on OQ3 and may be refused outright without affecting Phases 1–3.

Exit criteria, if pursued:

- The `test/test_link_unfurl.py:168` special-purpose-prefix table still passes
  unchanged: the default remains denial.
- A `100.64.0.0/10` address unfurls **only** when tailnet trust is enabled and
  the daemon confirms the address belongs to this tailnet.
- A `100.64.0.0/10` address the daemon does not recognise is still rejected.


## Migration and compatibility

Nothing changes for existing users by default: all flags default off, and with
no tailnet present every code path added here short-circuits on
`resolve_forwarded_peer() is None` and behaves exactly as today.

The four existing paths — direct loopback, SSH tunnel, chat presigned link,
Instances — are untouched. No config migration; no persisted-format change
beyond the peer-key string, which lives in the in-memory session pin state and
is regenerated on restart.

## Security considerations

- **Header spoofing.** The failure mode to avoid is a proxy forwarding an
  attacker-supplied `X-Forwarded-For` verbatim and a backend trusting it. §2
  never uses the header as the source of truth: the daemon resolves the peer and
  the header is only checked for agreement.
- **Fail-closed on ambiguity.** Every unresolvable case returns `None` and falls
  back to token auth. No branch grants access on a partial match.
- **Denial of service.** Resolution calls a local daemon. Bounded cache, hard
  timeout, and one resolution per WebSocket upgrade rather than per frame.
- **The tailnet ACL joins the trust boundary.** With `trust_identity` on, a node
  that can reach the Serve endpoint and whose login is allowlisted gets in. This
  is why §3 makes the allowlist mandatory, and it must be stated plainly in the
  guide: a shared corporate tailnet is not a private network.
- **Blast radius is bounded by §5.** Even a fully compromised tailnet identity
  cannot read stored credentials or rewrite channel config through the
  dashboard.
- **Net effect on the status quo.** Behind Serve today the pin is inert and the
  audit caller is the proxy. Phase 3 replaces both with a verified identity, so
  the change is a net tightening; Phase 2 alone is neutral on auth but raises
  reachability, which is why it may not ship without Phase 1's warning already
  in place.

## Alternatives considered

**Recommend `KIROCREW_BIND=<tailnet address>` instead of Serve.** Works today
and gives a genuine per-peer `request.remote`, so the IP pin functions. Rejected
as the recommended path: no TLS, requires a stable tailnet address in an env
var, and no identity headers — so the audit trail still cannot name a person.
Supporting both shapes as first-class would mean maintaining two sets of
forwarded-request semantics. It stays documented as a fallback.

**Trust `Tailscale-User-Login` alone.** Rejected. This is the spoofing class
described above; a header is not a credential.

**A generic `trusted_proxies` + XFF-depth setting.** Rejected as the first step.
It cannot verify identity, only relay a claim, so it would not fix Problem 1 —
and getting the depth arithmetic wrong is itself a spoofing vector. §2's
interface is deliberately narrow, and Cloudflare Access (a signed JWT, also
locally verifiable) is the natural test of whether it generalises. Do that when
there is a second consumer, not before.

**Support Funnel.** Rejected as a goal; see Non-goals.

**Do nothing.** Viable — Serve already works. Rejected because the guide would
keep recommending the public mode as the tailnet option and keep offering the
IP pin as a mitigation that does not hold.

## Open questions

1. **Is inferring auto-origin from `tailscale serve status` (§4) acceptable**, or
   must every widening of the origin allowlist be explicit config?
2. **Session TTL under per-request identity verification.** The 20-hour ceiling
   exists because a bearer token is all that stands between a leak and access.
   If identity is re-verified from the daemon on every request, is the ceiling
   still the right instrument?
3. **Should §6 exist at all?** It is the only phase that widens egress.
4. **Windows.** The `tailscale` CLI location and the daemon's local API on
   Windows are unverified. If resolution is not reliable there, Phase 3 is
   POSIX-only and must degrade to token auth rather than break.

## Resolved during review of this document

- **Pin scope: node or login** (was OQ1). **Resolved: configurable, defaulting to
  `node`** — see §3.1. `node` is the default because the flow it costs (one link,
  two devices) does not work under today's IP pin either, so it takes no working
  behaviour away, whereas defaulting to `login` would be a relaxation. `login`
  stays available for operators who re-enroll devices often. One hazard surfaced
  while settling this and became a hard rule rather than a caveat: an ACL tag
  replaces the user identity on a device, so every tagged node reports the login
  `tagged-devices`, which would make `login` scope collapse the pin across an
  entire tagged fleet — a tagged node is therefore always pinned to `node`
  regardless of configuration.
- **Scope of the guide correction** (was OQ6). Asked whether Phase 1 should
  correct the inert-pin claim only for the Tailscale rows or for every tunnel the
  guide recommends. **Resolved: every tunnel.** The claim is equally false for
  cloudflared and ngrok — both run on the gateway host and connect from
  loopback — and fixing all three costs nothing over fixing one. Phase 1's exit
  criteria were widened accordingly.
- **Whether the Phase 2 → Phase 3 coupling should be a hard precondition.**
  **Resolved: no.** Phase 2 ships with Phase 1's warning in place and Phase 3
  tracked; landing together is preferred, not required. Phase 3 can stall on OQ4
  (Windows), and an origin fix gated on an auth redesign tends to ship as neither.

## Provenance

Derived by reading the tree at `caa0dca7` in a single dashboard session — not by
a model panel, and not adversarially reviewed. Every file:line reference above
was checked against that commit.

Two findings emerged during that reading and changed the design rather than
decorating it: the IP pin is inert behind *every* recommended tunnel and not
just Tailscale (which reframed identity pinning from a feature to a defect fix,
and coupled Phase 2 to Phase 3), and "any tailnet member" would have been a
silently-permissive default on a shared corporate tailnet (which made the login
allowlist mandatory in §3). An earlier draft of this plan also placed the unfurl
change in the first phase as a small safe fix; it was moved to Phase 4 on the
grounds that it is the only proposal here that widens egress.

The design is therefore unvalidated by anyone but its author, and Phase 3 should
not land without adversarial review.

One review round has since run on the document PR. The GPT and Opus code lanes
returned no findings; the advisory Design Review verified the two central claims
against the tree independently (that `check_token_ip` compares against the value
captured at `token_auth.py:1506`, and that the guide presents Funnel as the
tailnet-private option while offering the IP pin as a mitigation it cannot
deliver behind a same-host proxy) and returned PASS with two suggestions. Both
were accepted and are recorded under "Resolved during review" above. That round
reviewed the *document*; it is not the adversarial review Phase 3 owes.
