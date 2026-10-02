# Outbound Instance Transport Implementation Plan

> **Nothing implemented.** This plan is unstarted at `18f9984b0`: the only
> `connection_method` values are `("ssh", "ssm", "fargate")`
> (`src/kiro_crew/instances/registry.py`), and
> `src/kiro_crew/instances/transports/` does not exist. Its spec is
> [`../rfc-outbound-instance-transport.md`](../rfc-outbound-instance-transport.md).

> **For agentic workers:** implement task-by-task. Steps use checkbox (`- [ ]`)
> syntax for tracking. Checkbox state is a progress record, not a status — read
> the RFC's `status` for that.

**Goal:** Reach a remote crew with no SSH key, no inbound listener on the remote
host, no dependency on an interactive cloud-session API, and no ability to
execute arbitrary code on the peer — by having both sides dial out to a
rendezvous service and moving the token mint from a remote exec to a channel RPC.
For an owner whose policy forbids SSH and SSM this is the only usable transport,
so the goal includes making the forbidden methods **fail closed** rather than
merely go unused.

**Architecture:** A `PeerTransport` protocol under
`src/kiro_crew/instances/transports/` replaces the per-method branching inside
`SshTunnelManager`. `outbound` is a fourth implementation whose reach is an
in-process loopback listener tunnelling framed HTTP over a persistent WSS channel.
`SshTunnelManager.proxy_request`
(`src/kiro_crew/instances/ssh_tunnel_manager.py`) stays the only way to talk
to a peer, so no caller changes.

**Sequencing rule:** phase 2 must land and be green before phase 3 starts. A
fourth branch added to the existing chains before the seam exists is the outcome
this plan is arranged to prevent. That applies to the dashboard's own branching as
well as the gateway's, which is why the exhaustive presentation mapping is in
phase 2: a method that exists before its surfaces are exhaustive renders as SSH.
Phase 3's posture controls, posture row and locale strings ship in the same PR as
the transport, not after it.

---

## Phase 1 — Land the design (this PR)

- [ ] `docs/request-for-change/rfc-outbound-instance-transport.md`
- [ ] Index row in `docs/request-for-change/README.md`
- [ ] This plan, plus its row in `docs/request-for-change/plans/README.md`
- [ ] Resolve open question 1 (does the reference service ship in-tree) with the
      maintainers before phase 4 is scheduled. This is **blocking** for a
      policy-constrained owner, so the viable answers are in-tree or an app
- [ ] Resolve open question 3 (which sealing primitive) with a reviewer who has a
      cryptography opinion, before phase 5

## Phase 2 — Extract the transport seam (refactor, with two stated behaviour changes)

- [ ] Create `src/kiro_crew/instances/transports/__init__.py` with the
      `PeerTransport` protocol: `validate`, `open`, `mint`, `describe_target`,
      `diagnose`, `restart`
- [ ] Move the `ssh` arm of `_resolve_transport`
      (`src/kiro_crew/instances/ssh_tunnel_manager.py`) into
      `transports/ssh.py`
- [ ] Move the `ssm` arm, including the ECS-target refusal, into
      `transports/ssm.py`
- [ ] Move the `fargate` arm, including its "no token to mint" answer from
      `_mint_for` (`src/kiro_crew/instances/ssh_tunnel_manager.py`), into
      `transports/fargate.py`
- [ ] Replace the `method ==` branches in connect, both self-heal tiers,
      diagnostics and restart with calls through the resolved transport
- [ ] Keep `_TransportParams` as the validated-value object the transports
      return, so the tunnel-spawn call site is untouched
- [ ] Run the existing instance tests with **no edits**; any required edit means
      the refactor changed behaviour and must be reworked
- [ ] Add a test asserting the transport registry's keys are exactly
      `("ssh", "ssm", "fargate")`, so phase 3 has to extend it deliberately

### Phase 2 dashboard surfaces — the same seam, one layer up (RFC §3.9)

The dashboard branches on `connection_method` with its own non-exhaustive
conditionals whose last arm is `ssh`. Fixing that here, while only three methods
exist, is what stops phase 3 from shipping a record that renders as SSH.

- [ ] Introduce one exhaustive method→presentation mapping in
      `website/src/utils/remoteCrew.ts`, keyed by method, carrying the badge
      label key, the hint key and which addressing field the card should print
- [ ] Replace `connectionTypeLabel` and `connectionTypeHint`
      (`website/src/pages/settings/RemoteCrewPanel.tsx`) with lookups through it,
      so an unmapped method renders visibly unmapped instead of inheriting the
      SSH label and the SSH hint
- [ ] Replace the card's `ssm_target`-or-`ssh_host` target expression with the
      mapping's addressing field, so an unmapped method shows no field rather
      than an empty SSH host
- [ ] Route both diagnostics handoffs through the mapping: the one in
      `RemoteCrewPanel.tsx` and the inline `=== 'ssm' ? 'ssm' : 'ssh'` in
      `website/src/components/InstancesViewport.tsx`, which does not use the
      shared helper at all
- [ ] Three further surfaces branch on the method with `ssh` as their last arm and
      are not reached by the two helpers above, so the claim that this section
      stops a record from rendering as SSH is false until they route through the
      mapping too: the row badge and target in
      `website/src/pages/settings/InstancesPanel.tsx` (prints the literal `'SSH'`),
      the tab-row target in `website/src/components/InstanceTabBar.tsx` (falls back
      to `ssh_host`), and the selector hint in
      `website/src/pages/settings/InstanceFormFields.tsx`
- [ ] **Second intended behaviour change, and the one with teeth:**
      `InstanceFormFields.tsx` collapses an unrecognised `connection_method` to
      `'ssh'` when it loads a record into the form, so opening an unmapped crew for
      edit and saving it **rewrites its method** — data loss, not a label defect.
      Make that arm exhaustive and add the regression test of record: loading a
      record whose method the form does not know leaves the method unchanged on
      save
- [ ] **Intended behaviour change, stated rather than absorbed:** this corrects an
      existing defect — a `fargate` crew's failure report says `transport: ssh`
      today. Add it as the regression test of record: a `fargate` instance's
      report names `fargate`
- [ ] Add a test that feeds an unregistered method through every surface in this
      mapping and asserts it acquires no sibling's badge, hint, target field or
      repair steps
- [ ] Run the existing `website` instance and remote-crew tests; only the
      `fargate` diagnostics expectation above may change

## Phase 3 — Outbound transport client (default off)

- [ ] Add `hub_url`, `peer_handle`, `enrolment_ref` to `Instance`, defaulting to
      empty, with validation in the new `outbound` arm only
- [ ] Extend `CONNECTION_METHODS` to include `"outbound"`, and add the addressing
      fields to the immutability set in
      `src/kiro_crew/dashboard/handlers_instances.py`
- [ ] Confirm a registry file written before this change loads unchanged
      (round-trip test on a fixture record with no new fields)
- [ ] Implement the frame codec: `REGISTER`, `HEARTBEAT`, `OPEN`, `DATA`,
      `CLOSE`, `MINT`, `RESTART`, `EVENT`, `CANCEL` — nine types, each with
      instance identity, correlation id, expiry and verified sender, and none
      carrying a command, path, user name or shell argument
- [ ] Implement the in-process loopback listener: bind `127.0.0.1:<local_port>`
      from the allocator, translate each HTTP request into `OPEN`/`DATA`/`CLOSE`
      and stream the response back
- [ ] Assert no child process is spawned for an `outbound` instance, and that
      `forwarder_pid` / `forwarder_sig` stay unset
- [ ] Implement `mint` as a `MINT` frame round-trip; store the returned token
      through the existing store and let the existing refresh timer drive it
- [ ] Map heartbeat liveness onto `TunnelStatus`, resolving open question 2
      (stale-but-connected: new state or error)
- [ ] Implement reconnect with bounded backoff, and teardown that closes the
      channel and releases the port
- [ ] Config section for the transport, default off; document the default in the
      config reference

### Phase 3 posture controls — ship with the transport, not after it

A transport that lands before its controls invites exactly the record it is meant
to refuse, so these are in the same PR (RFC §5).

- [ ] Add `allowed_methods` to `InstancesConfig`
      (`src/kiro_crew/config/sections.py`), defaulting to every registered
      method so existing installs are unchanged
- [ ] Enforce it in `Instance.__post_init__`
      (`src/kiro_crew/instances/registry.py`) so a forbidden method is refused
      at validation — not at connect — and cannot be created or loaded
- [ ] Test: with `allowed_methods=["outbound"]`, creating or loading an `ssh`,
      `ssm` or `fargate` record fails, and the add-instance API returns a refusal
- [ ] Add the pinned rendezvous-host allowlist to the same config section, and
      refuse a `hub_url` outside it in the same validation path
- [ ] Test: an agent-written record naming an unlisted host fails validation
- [ ] Honour the standard HTTP `CONNECT` proxy environment for the WSS dial and
      its TLS handshake
- [ ] Accept a corporate CA bundle by explicit configuration rather than relying
      on the ambient trust store
- [ ] Test that neither the proxy setting nor the CA bundle is silently ignored
- [ ] Diagnose egress-blocked, service-down and enrolment-rejected as three
      distinct outcomes, with a test on the diagnosis output

### Phase 3 posture visibility — where a reviewer looks (RFC §5.7)

`kirocrew doctor` is not the surface a review reads. The dashboard already renders
a row per security control, so the effective posture is registered as one.

- [ ] Add a control to `_CONTROLS` (`src/kiro_crew/security_posture.py`) whose
      items are the `connection_method` values `allowed_methods` currently
      permits, with the enforcing module as its `source`
- [ ] Report `count` as unresolved rather than permissive when the effective
      posture cannot be determined, so the row can never claim all four methods
      because it failed to read the config
- [ ] Test: with `allowed_methods=["outbound"]` the control's items are exactly
      that one method, and the count matches `items` length
- [ ] Test: an unreadable or malformed config yields the unavailable state, not a
      permissive list
- [ ] Confirm no frontend change is required — the panel's icon map falls back to
      a generic shield for an unknown control key — and add an icon entry only if
      the generic row reads poorly beside its siblings

### Phase 3 locale strings — a record that exists must be labellable

Phase 2 makes the mapping exhaustive; this gives the new entry something to print.

- [ ] Add all **three** strings for `outbound` to every shipped locale catalog
      (`website/src/i18n/locales/`, thirteen catalogs), following what `fargate`
      actually landed rather than the two-key reading: the badge label
      `pages.settings.remoteCrewPanel.type_outbound`, the card hint
      `pages.settings.remoteCrewPanel.transport_hint_outbound`, and the form's
      method hint `pages.settings.instancesPanel.outbound_method_hint` — a
      different namespace and a different naming scheme, and the one key `fargate`
      has no `ssh`/`ssm` sibling for, so it is the one this phase will otherwise
      ship without
- [ ] Re-snapshot the untranslated-strings baseline if the gate requires it, and
      confirm the i18n tests pass rather than assuming they are unaffected

## Phase 4 — Peer-side agent

- [ ] Peer-side dispatch acting on exactly three inbound verbs — forward HTTP to
      its own loopback dashboard port, mint its own token, restart its own gateway
      unit — and refusing everything else
- [ ] Test asserting the inbound dispatch table is exactly those three verbs, so a
      fourth capability cannot be added without failing a test. Assert the verb
      set, not the codec's frame count: a codec may grow a frame type harmlessly
- [ ] Local mint called in-process — the same path `kirocrew token` uses — with
      no shell and no `sudo`
- [ ] `RESTART` handling: restart the peer's own gateway unit, no arguments
      accepted, refusal when the unit is unknown. Replaces the command-line
      dispatch in `restart_remote`
      (`src/kiro_crew/instances/ssh_tunnel_manager.py`) for this method
- [ ] Supervise the agent independently of the gateway (its own unit), so a
      stopped or broken gateway leaves the channel up — the break-glass
      requirement of RFC §5.6
- [ ] Test: the agent answers `RESTART` while its gateway is stopped
- [ ] Primary enrolment: the peer reads its enrolment secret at first start using
      the identity it already carries, with no shell, no interactive session and no
      keystroke on the peer
- [ ] Test asserting that enrolment path spawns no shell and opens no interactive
      session
- [ ] Fallback enrolment: single-use code, one-shot exchange, key stored by the
      peer, code dead after exchange
- [ ] Revocation path, with a test that revoking one peer's key leaves the other
      peers connected
- [ ] Audit trail (RFC §3.8): one content-free append-only line per channel open,
      channel close, `MINT` and `RESTART`, and per refusal route — refused
      `REGISTER`, expired or replayed enrolment, sealing failure, verb refusal
- [ ] Test asserting the trail carries peer handle, correlation id, verb, outcome
      and timestamp, and carries no token, header or request path

## Phase 5 — Reference service and guide

**Mandatory, not optional, for an owner whose policy forbids SSH and SSM: without
a service they have no transport at all (RFC §5).**

- [ ] Rendezvous service reference implementation or deployment recipe, per the
      answer to open question 1 — noting that "guide-only" is not an available
      answer for a policy-constrained owner
- [ ] Sealed-payload layer per RFC §3.7, resolving open question 3 (which
      primitive), so the service routes on an envelope and cannot read a body
- [ ] Test asserting the service sees no token and no request path in a frame it
      forwards
- [ ] Protocol document precise enough for a third party to implement the
      service
- [ ] Guide section in `docs/guides/` covering enrolment, revocation and the
      threat model, cross-linking the tailnet RFC's token-pinning finding
- [ ] Guide: provider-side conformity checklist (RFC §5.5) — no key-pair resource,
      no `ssm:StartSession` / `ssm:SendCommand` on the instance profile or operator
      role, no inbound rule, enrolment secret readable only by the peer's identity
- [ ] Guide: the break-glass cost (RFC §5.6) stated plainly — a peer whose agent
      itself fails is replaced, not reached into
- [ ] Deployment note stating explicitly that the service can deny service and
      observe timing, sizes and peer handles, and cannot impersonate either side,
      read a payload, mint a token or trigger a restart

## Phase 6 — Owner-facing entry and diagnosis

Everything a phase-3 record needs in order to render correctly has moved earlier:
the exhaustive mapping to phase 2, the locale strings and the posture row to phase
3. What is left here is the owner's way *in* and the diagnosis of a channel that
misbehaves — work that genuinely has no consumer until a person adds an instance
by hand.

- [ ] Add-instance form gains the `outbound` option with its three fields
- [ ] Status strings and error copy for channel states, matching the existing
      per-method message style
- [ ] `kirocrew doctor` check: channel reachable, heartbeat fresh, enrolment not
      expired
- [ ] `kirocrew doctor` reports the effective method posture, as the CLI-side
      companion to the posture row landed in phase 3 — the row is what a review
      reads, the command is what an operator runs
- [ ] Add-instance form hides methods that `allowed_methods` forbids, with the
      validation refusal as the real control behind it
- [ ] Per-method diagnosis for `outbound`, alongside the existing
      `diagnose_instance_fargate` sibling

## Decisions recorded, so they are not re-litigated as omissions

- **`allowed_methods` gets no dashboard field** (RFC §5.2). A compliance switch
  writable through the surface it constrains is not a control, and the two files
  already sit in different protection classes: `config.json` is write-protected
  from the agent's file-edit tool, `instances.json` is agent-writable by design.
  Set once at fleet setup by the policy owner. Accepted costs: shell writes are
  not covered by that protection, and hand-editing config is a worse first run
  than a form.
- **No new UI panel.** The posture row reuses the existing registry and the badge
  reuses the existing card, so this RFC adds no settings surface of its own.

## Out of scope for every phase above

- Remote subagent placement — [#12821](https://github.com/kirodotdev/KiroCrew/pull/12821)
- A compute backend — [`../rfc-remote-instance-on-fargate.md`](../rfc-remote-instance-on-fargate.md)
- Private-network access — [`../rfc-tailnet-dashboard-access.md`](../rfc-tailnet-dashboard-access.md)
- A same-origin pane, which would delete the phase 3 listener —
  [#12229](https://github.com/kirodotdev/KiroCrew/pull/12229)
