# Google Drive knowledge connector (W03/W04 · D1)

The Google Drive provider stream's knowledge slice: a **real Drive v3 provider**
assembled as W01 operations, a **per-document `SourceRow`** ingest, a
**SyncScheduler** incremental/refresh path, and a **real-time per-query ACL
probe**. This is the owning doc for
`src/kiro_crew/connections/vendors/google_drive/` and
`src/kiro_crew/knowledge/connectors/google_drive.py`.

Scope note: every outbound Drive call runs through W01's control-plane
`execute` / `PageWalk`. This slice **assembles** (endpoint, params, schema,
pagination, error mapping) and **sequences** operations; it holds no token, opens
no session, sends no HTTP, and never copies vault/token-refresh/revoke. Credential
custody is single-sourced in W01 (PR #11286). It consumes, unchanged:

- the shared control plane's `Effect`, `ErrorClass`, `OperationDescriptor`,
  `RequestLocator`/`ResultDecode`, `execute`/`PageWalk` seam;
- the shared per-row ACL contract — `SourceRow`, `ProviderResourceRef`,
  `RevalidationHook` — from the shared-KB query-time ACL work (PR #11219);
- the governance `SCOPE_CATALOG`: a Drive operation is governed under the
  existing `tools` scope with the operation id as the governed item. **No new
  scope is registered.**

## What this stream owns, and what it consumes

| Owns (here) | Consumes (elsewhere, unchanged) |
|---|---|
| Per-operation Drive `OperationDescriptor`s (`descriptors.py`) | The descriptor schema + `Effect`/`ErrorClass` closed sets |
| Drive v3 request assembly — URL/params/headers — as a `RequestLocator` (`locator.py`) | W01's transport, credential custody, redirect guard |
| 2xx→`OperationResult` decode with `CollectionPayload`/`ObjectPayload`/`BytesPayload` + `nextPageToken`→`next_cursor` (`decode.py`) | The neutral result envelope + the one-cursor invariant |
| Drive failure→neutral class mapping (`errors.py`) | The `ErrorClass` vocabulary |
| Sequencing: list walk, export/media/shortcut selection, change feed + resync (`operations.py`) | W01's `execute`/`PageWalk` (the runner is injected) |
| Per-file `SourceRow` + `ProviderResourceRef` + subject derivation (`knowledge/connectors/google_drive.py`) | The per-row ingest pipeline, `SourceRow.__post_init__` contract |
| Real per-query ACL probe → `FRESH`/`REVOKED`/`UNVERIFIABLE` (`acl_probe.py`) | The retriever's `RevalidationHook` seam + the store's grant lookup |

## The four segments

1. **Real provider.** `drive_api.py` builds `files.list`/`files.get`/`export`/
   `alt=media`/`changes.list`/`changes.getStartPageToken`. Every list request
   sets **both** `supportsAllDrives=true` **and** `includeItemsFromAllDrives=true`
   (omitting either silently searches only My Drive). A drive-scoped list sets
   `corpora=drive` + `driveId`. Native Docs/Sheets/Slides go via `export`; binary
   via `alt=media`; a `application/vnd.google-apps.shortcut` is resolved by
   `shortcutDetails.targetId` (one hop, no shortcut→shortcut chase) before its
   content is fetched.

2. **Per-document `SourceRow`.** One Drive file → one row, keyed by `fileId`,
   carrying `driveId`/`mimeType`/`modifiedTime`/`version` and a content
   fingerprint (the row text is prefixed with `version` + `modifiedTime`, so a
   metadata-only change such as a re-share re-ingests and the ACL is re-derived).
   Never aggregated into one blob. Folders and trashed files yield no row.

3. **SyncScheduler / refresh.** The saved checkpoint is read from the source's
   `properties` JSON (where the shared scheduler's `_advance_checkpoint` writes
   `props["checkpoint"]`), NOT from a top-level key — a bare top-level read is
   `None` every round on a raw `sources` row and would silently degrade every
   sync to a full snapshot. No checkpoint → snapshot (`files.list` over My Drive +
   every Shared Drive, then a fresh `startPageToken` as the resume checkpoint,
   `snapshot=True`). Checkpoint present → incremental via `changes.list`:
   - **Stale token** → resync boundary (re-fetch the start token) → full-snapshot
     reconcile (`snapshot=True`) so no file is missed.
   - **A removal (or trashed file) in the batch** → full-snapshot reconcile NOW
     (`snapshot=True`), because the shared pipeline deletes only on a snapshot and
     an incremental round cannot express deletion (`absent != deleted`). This uses
     the existing `_snapshot` path and the SHARED delete protocol — no second sync
     mechanism, no vendor-side delete channel. The fresh batch checkpoint is still
     advanced so the removal is not re-processed.
   - **A metadata fetch failure** for a changed file → the round returns the
     ORIGINAL checkpoint (no advance), because the shared scheduler advances the
     checkpoint on a "fully persisted" round and a `continue`-skipped change never
     becomes a row; advancing would skip that change permanently. The next sync
     re-reads the same batch and re-attempts.
   - Otherwise → a row per changed file (`snapshot=False`), advancing to the
     batch's `newStartPageToken`. Duplicate change records for one file in a batch
     collapse to the last state (one row, not two).

4. **Real-time per-query ACL probe.** The query identity is verified per query
   through W01's execute path; there is no self-asserted-email impersonation.
   Revocation is effective on the **next** query. Ingest never invents a grant:
   the subject set is the file's **real** permission principals; an explicit
   `type == "anyone"` permission is honoured as public; a **missing** permissions
   field is an EMPTY subject set (deny-all), never defaulted to public. The
   query-time probe is authoritative regardless.

## Documentation gap: change-token resync trigger

Google's change-management guide documents the resync **flow** (re-fetch
`startPageToken` and resume) but pins **no TTL and no exact HTTP status/reason**
for an expired change page token. Per the brief, no number or status code is
invented.

Two consequences the implementation records rather than papers over:

- **Recovery is "re-fetch the start page token = resync boundary."** On a resync
  the connector returns empty changes + the fresh token and reconciles via a full
  snapshot.
- **The live trigger is the neutral error CLASS, not a reason subcode.** W01's
  executor redacts the provider's `reason` text before a vendor sees it (a
  security boundary this slice does not fight) and does not carry the raw HTTP
  status onto the error outcome. So `operations._is_stale_page_token` keys on
  W01's `input` class: for an incremental `changes.list` the vendor itself builds
  (fixed endpoint, valid params, a server-issued token) the only realistic
  determinate client-error is a token the server no longer accepts. Every
  **non-`input`** failure (auth/forbidden/throttle/not_found/temporary) still
  raises and is never swallowed as a resync. `errors.is_stale_page_token` remains
  as the documented reason-level Drive-shape classifier for a direct caller that
  still holds the raw failure (or a future W01 surface that carries the status),
  and records the Drive fact without asserting a published code.

## Registration (proposal — shared handler is the ACL owner's)

The shared knowledge handler
(`src/kiro_crew/dashboard/handlers/knowledge.py`) is **not** edited by this
stream. That handler already carries the ACL owner's landed runner-injection seam
(`_register_optional_connector`), which registers `google_drive` and constructs
`GoogleDriveConnector(<runner_factory>)`, reading the factory from
`app["knowledge_connector_runners"]`. The production factory that composes that
runner IS shipped by this stream:
`connections/vendors/google_drive/host_factory.py::make_drive_operations_factory`
builds the full W01 path into the `source -> DriveOperations` callable the
connector expects. It **RESOLVES an already-trusted binding** from the live
`BindingStore` (never mints one), so a REVOKED source fails loudly on the next
sync instead of being resurrected (revocation isolation). Every authority input
is host-supplied, never defaulted in vendor code: `granted_scopes` (the real
stored grant), the governance `layers`, a required live `clock`, and
`ttl_seconds`; the credential is addressed by the resolved binding's own scoped
`secret_ref`, not a provider-slug family. It is proven executable end to end in
`test/test_google_drive_host_factory.py` (a real `files.list` walk over a real
vault + live store + scripted socket, the vault credential asserted on the
request; plus revoked-source-fails-loud and absent-binding-fails-loud
regressions). The single remaining host step — a one-line install of that factory
into `app["knowledge_connector_runners"]` with the host-owned
verifier/store/vault/authority — is documented in
`connector-google-drive-registration-proposal.md`; it needs W01's executor
(PR #11286) at runtime. The query-time ACL revalidator (`acl_probe.GoogleDriveRevalidationHook`)
installs on `app["knowledge_revalidator"]` the same way. An empty registration
does not count as `code_complete`; the factory is a real, executable product, not
an injection stub.

## Verification & ceiling

No Google fixture account exists, so the ceiling is `code_complete`. Every path
is proved against the **real** W01 `execute`/`PageWalk` over a scripted transport
that runs the real `locator.locate` + `decode.for_operation` — the vendor
assembly and the control-plane gate chain are both exercised; only the socket is
scripted. Negative coverage: malformed responses, missing permissions, token
resync, duplicate cursor, multi-page walk, removed change, content-fetch failure,
and revoke-then-deny on the next query. No live evidence is fabricated and no real
business write is performed.
