# Registration — status + the one remaining host-side hook

**Update (base advanced):** the shared knowledge handler now carries a landed
**runner-injection registration seam** (commit `40caf8031`, on this PR's base
`feat/connector-control-plane-executor`), authored by the ACL/handler owner. It
already registers `google_drive` by module + class, so there is **no handler edit
to propose** and **no from-scratch hunk**. This doc records what the seam does,
confirms my connector satisfies its contract, and states the ONE host-side hook
that remains (which needs W01's executor and is the host's, not mine).

## What the landed seam already does (not mine to change)

`src/kiro_crew/dashboard/handlers/knowledge.py`:

- `_register_optional_connector(connectors, module_path, class_name, runner_factory)`
  imports the vendor module (guarded) and constructs the connector **with** the
  injected runner: `connector = connector_cls(runner_factory)` (positional).
- `setup_knowledge_routes` reads `app["knowledge_connector_runners"]`
  (a `{source_type: runner_factory}` map the host installs once W01's executor —
  PR #11286 — is available) and registers each vendor **only when** its module
  imports **and** its runner factory is installed. Absent module or absent runner
  ⇒ not registered (fail-closed, never public).
- The loop already lists my connector:
  `("google_drive", "kiro_crew.knowledge.connectors.google_drive", "GoogleDriveConnector")`.

## My connector satisfies that contract (verified)

- `GoogleDriveConnector.__init__(self, operations_factory=None)` — a positional
  `connector_cls(runner_factory)` binds `operations_factory = runner_factory`.
- `fetch_rows` calls `self._operations_factory(source) -> DriveOperations`, so the
  installed factory must be `Callable[[source_dict], DriveOperations]`.
- `validate_config` returns False when no factory is wired, so a no-arg
  registration is impossible-by-construction — matching the seam's fail-closed
  intent (an empty registration is not `code_complete`, and the seam enforces it).
- Verified live: `test/test_knowledge_query_acl_wiring.py` +
  `test/test_knowledge_rows_ingest.py` (the seam's own tests, on this base) pass
  with my connector in the tree.

## The ONE remaining host-side hook (host/ACL owner, needs W01 PR #11286)

Install a `google_drive` runner factory into `app["knowledge_connector_runners"]`.
The factory itself is a **shipped product**, not a proposal:
`kiro_crew.connections.vendors.google_drive.host_factory.make_drive_operations_factory`
composes the whole W01 path and returns the `source -> DriveOperations` callable
the connector expects. It **RESOLVES an already-trusted binding** from the live
`BindingStore` (`BindingStore.resolve`) — it does NOT mint one, so a REVOKED
source raises on the next sync instead of being silently resurrected (revocation
isolation). Proven executable end to end in `test/test_google_drive_host_factory.py`
(a real `files.list` walk over a real AES-GCM vault + live store + scripted socket;
plus a revoked-source-fails-loud regression and an absent-binding-fails-loud one).

The factory carries NO authority of its own — every authority input is
host-supplied, none defaulted in vendor code:

- `granted_scopes` — the scopes the REAL stored grant carries (not a vendor
  assertion). `requested_scopes` keeps a read-only minimization default only.
- `layers` — the host's five-layer governance `LayerCeilings` (no vendor-built
  empty ceilings that would bypass governance).
- `clock` — a live time source, REQUIRED (no frozen-instant fallback that would
  make TTL/expiry checks vacuous).
- `ttl_seconds` — the handle lifetime.

The credential is addressed by the RESOLVED BINDING's own scoped `secret_ref`
(per-binding, via W01's store + transport) — never a provider-slug vault family.

So the host hook is a single call supplying the host-owned dependencies and
authority (none of which the vendor package holds):

```python
# host setup, once the W01 executor is available:
import time
from kiro_crew.connections.vendors.google_drive.host_factory import (
    make_drive_operations_factory,
)

app.setdefault("knowledge_connector_runners", {})["google_drive"] = (
    make_drive_operations_factory(
        verifier=<host SubjectTenantVerifier>,       # real Google-side identity check
        binding_store=<the live BindingStore>,       # L04 store (bindings the host admitted)
        vault=<the SecretVault>,                      # existing custody
        deployment_id=<this deployment's id>,
        kiro_principal=<the authorized Kiro principal>,
        clock=time.time,                              # live clock, required
        granted_scopes=<the real stored grant's scopes>,  # authority, not a default
        layers=<the host's governance LayerCeilings>,      # governance, not empty
        ttl_seconds=<handle lifetime>,
    )
)
```

The landed `_register_optional_connector` loop then constructs
`GoogleDriveConnector(<that factory>)` and registers it (fail-closed until the
factory is installed). No handler edit; no second host assembly point; the factory
holds no token/session/HTTP and copies no W01 code — custody stays single-sourced
in W01.

The host-owned pieces named honestly (still the host's, not vendor work): the
`SubjectTenantVerifier` (real identity verification against Google) and the live
`BindingStore` + `SecretVault` wiring. `host_factory` composes them; it does not
invent them.

## The query-time ACL revalidator (segment 4) — same pattern, host install

`app["knowledge_revalidator"]` (read by the retriever) should be set to
`kiro_crew.connections.vendors.google_drive.acl_probe.GoogleDriveRevalidationHook(
store=<store>, runner=<subject-scoped SubjectOperationRunner>)`. The runner runs
`files.get` AS the querying subject (no impersonation; a subject with no binding
→ UNVERIFIABLE; revocation effective next query). The hook already resolves the
item's `ProviderResourceRef` by `item_id` via `store.get_item_grants` and persists
via `store.revoke_item_acl` / `store.mark_item_acl_revalidated` (all verified to
exist).

## Not proposed / not touched

No change to W01 `control_plane/**`/`exports`, container anchors, `setup.cfg`,
workflows/permissions/hooks, S2 validator, fast-gate, or the main manifest. No
new governance scope. No live-secret read, no real Google request, no business
write.
