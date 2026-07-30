# RFC: Official App Registry + Editorial Feed

**Author:** KiroCrew contributors
**Date:** 2026-07-29
**Status:** Draft

---

## 1. Problem Statement

The App Store's catalog and its merchandising are frozen into each KiroCrew
release. Two concrete gaps:

1. **No first-party remote registry.** The curated catalog is the bundled
   `kiro_crew/apps/app-registry.json`, compiled into the wheel next to
   `registry.py` (`_REGISTRY_FILE`). Changing the catalog — adding an app,
   fixing a repo URL, pulling a broken one — requires shipping a new app
   release. User-configured *external* registries exist
   (`ExternalRegistryConfig`: `name`/`repo`/`branch`, git-clone based) but are
   deliberately **untrusted**: `pickFeatured()` in `AppsPage.tsx` drops every
   `_registry`-marked entry (`a => !a._registry`), and install/browse clone
   them credential-free behind the SSRF + kebab-case + subdirectory-containment
   gates. There is no source that is both *remote* (updatable out of band) and
   *trusted* (allowed to drive featuring).

2. **No official editorial recommendation, and no versioned presentation
   contract.** "Featured" today is a `featured` flag/number on **bundled**
   entries plus a client-side `pickFeatured()` heuristic — baked into the
   frontend and the shipped index. There is no hosted, curator-driven feed for
   the Discover page, and no schema version on the store's presentation layer,
   so we cannot change *how the App Store page looks or works* without shipping
   a new client. (The only `schemaVersion` in `apps/` is on the installed-app
   record in `manager.py`, unrelated to the catalog or the store UI.)

### Goals

- A KiroCrew-owned **official registry** the client pulls at runtime, trusted
  enough to drive featuring, updatable without an app release.
- A KiroCrew-owned **editorial feed** describing the Discover page
  declaratively, that is **fail-safe** (degrades gracefully when unreachable or
  malformed) and **schema-versioned** (new layouts reach new clients while old
  clients keep working).
- Both decoupled from the app-release cadence, with a clean curation trust
  boundary.
- An **effective removal path**: pulling an app must take effect without a client
  release, and must survive the stale-cache fallback.
- Room for **multiple hosting sources** (internal git farms, artifact stores /
  S3, OCI, other internal registries) without a breaking schema change.

### Non-goals

- The app *loading* model (ESM/import maps, bundle hashing). That is
  [rfc-federated-app-platform](./rfc-federated-app-platform.md); this RFC is a
  companion covering the **source of the catalog + its merchandising**, not how
  an app's UI is loaded. **Precedence:** where the two overlap on the wire format,
  this RFC's document envelope and entry schema (§3) supersede that RFC's §3.7
  registry sketch — integer `schemaVersion` replaces its string `"version"`, entry
  display fields stay out of the index (they come from each app's `app.json`), and
  its flat `bundleUrl` + `bundleHash` map onto `source.type: "bundle"` with
  `integrity`. Its trust-tier vocabulary is reconciled in §5.
- Third-party/community registry trust changes. User-configured external
  registries stay untrusted exactly as today.

---

## 2. Where things live

Split by layer, not lumped into one repo. The repo split is **decided**; the
snapshot-sync mechanism in the third row is still open (§8, *snapshot-sync
mechanism*):

| Layer | Home | Contents |
|-------|------|----------|
| **Contract (schema)** | `KiroCrewApps` *(now; migrates to `KiroCrewAppSDK` later)* | JSON Schema + generated TS types for the registry entry and the editorial document. Starts co-located with the data it validates; extracted to the SDK once there are external consumers (published app-author tooling). |
| **Data (source of truth)** | `KiroCrewApps` | `official-registry.json` + `editorial.json`, hand-curated. A publish CI workflow validates against the co-located schema and pushes to the distribution CDN. |
| **Client + fallback** | `KiroCrew` | Fetch / validate / layered-fallback code, **plus** the bundled fallback snapshot `kiro_crew/apps/app-registry.json`, which is **generated** from `KiroCrewApps` at build time (or a bot sync PR) — never hand-authored, so the offline floor cannot drift from canonical. |

Rationale: the entire premise of goal 1 is to decouple catalog + merchandising
cadence from app releases. Co-locating the data in `KiroCrew` re-couples them —
every catalog tweak or featured swap would go through the product repo's
CODEOWNERS, review, and commit history, and force curators (PM/devrel) to hold
product-code write access. A dedicated catalog repo gives a separate write/trust
boundary and a clean audit trail. Precedent: Obsidian's `obsidian-releases`
holds `community-plugins.json` + featured lists separate from the app; Homebrew
taps; Raycast's extensions repo. `KiroCrewApps` already exists (currently an
empty stub) as the intended home.

The schema **starts in `KiroCrewApps`, co-located with the data it validates**,
and migrates to `KiroCrewAppSDK` later. Its only consumer at launch is the
catalog's own validate-and-publish workflow; the client read-path and published
app-author tooling that would justify a separately-versioned SDK package aren't
wired yet. Keeping it next to the data avoids a premature cross-repo release
dependency and a version-skew surface while the contract is still churning. The
migration is mechanical (move the schema files, publish them from the SDK,
repoint the validator import) and the tolerant-reader/additive rules in §4 keep
the on-the-wire `schemaVersion` stable across the move — so nothing consuming a
published doc has to change when the schema's *home* changes.

### Authoring surface ≠ serving surface (generated publish)

The CDN artifact is **generated by CI from the curated files**, not the curated
file copied verbatim. Curators edit a human-friendly source of truth in
`KiroCrewApps`; the publish workflow emits the machine-facing document:

```
KiroCrewApps (authored, reviewed)
   → CI: validate against schema · normalize · resolve · stamp · integrity · SIGN
      → published document on the CDN (generated, immutable per revision)
         → client fetch  /  generated bundled snapshot in KiroCrew
```

What the generator adds that hand-authoring cannot guarantee:

- **schema validation as a merge gate** — an invalid catalog can't reach clients;
- **normalization** — legacy flat entries folded to the tagged `source` union
  (§3.1) once, at publish time, so the client's compatibility path is a thin
  safety net rather than the main road;
- **`generatedAt` / revision stamping** and a digest, so a client (and the
  bundled-snapshot generator) can tell two payloads apart;
- **referential checks** — every editorial `appRef` and every `replacedBy`
  resolves to a live entry, and no entry is simultaneously listed and tombstoned;
- **the detached signature** over the published bytes. Signing is part of this
  pipeline from the first publish (§7 step 1), not a later addition — the client
  refuses to grant Official trust to an unsigned document (§5), so an unsigned
  publish would simply produce a feed nobody honors.

Precedent: Homebrew authors formulae in a git tap and serves a *generated* JSON
API from `formulae.brew.sh`; the tap is never the read surface. Same split here.

Two consequences worth stating: published documents must be treated as
**immutable per revision** (never rewrite a published payload with different
bytes — that is the CloudFront edge-skew failure mode the release system already
learned), and the bundled fallback snapshot in `KiroCrew` is generated from the
*published* document, not from the authored file, so the offline floor is
byte-consistent with what the CDN serves.

---

## 3. Registry schema

The registry stays a **minimal index**: identity + install metadata only. All
display fields (`displayName`, `description`, `screenshots`, `heroImage`,
`tags`, `highlights`, ...) come from each app's own `app.json`, fetched and
cached on demand. This is the existing "single source of truth" design — app
authors never edit the registry to change their description.

The canonical published schema is **generic and host-neutral** — no
Amazon/Brazil constructs.

### 3.1 Entry shape — tagged `source` union

Where an app's bytes come from is a **discriminated union** under `source`, not a
set of flat sibling URL fields. Only `type: "git"` is defined in v1; the
discriminator exists from day one so a second transport is additive:

```jsonc
// one registry entry (canonical, public)
{
  "name": "oncall-radar",            // unique id, lowercase kebab-case
  "source": {
    "type": "git",                   // the enum
    "url": "https://github.com/acme/oncall-radar.git", // any git-cloneable URL
    "ref": "3f2a1c9…",               // MUST be an immutable commit id (see below)
    "subdir": ""                     // optional
  },
  "resources": "app",                // where it runs: "app" (its own) | "gateway"
  "lifecycle": "app",                // who manages it: "app" | "gateway"
  "detectInstalled": "test -d ~/Applications/OncallRadar.app" // optional probe
}
```

Reserved variants, each carrying **its own** pinning/auth fields — which is the
whole reason for the union, since a `sha256` is meaningless for git and a `ref`
is meaningless for a tarball:

```jsonc
{ "type": "archive", "url": "https://…/app-1.2.0.tar.gz", "sha256": "…" }
{ "type": "s3",      "bucket": "…", "key": "…/app-1.2.0.tar.gz", "region": "…", "sha256": "…" }
{ "type": "oci",     "ref": "registry.example/acme/app:1.2.0", "digest": "sha256:…" }
{ "type": "bundle",  "url": "https://…/index.mjs", "integrity": "sha384-…" }
```

Notes that make this work in practice:

- **Internal git farms need no new type.** `type: "git"` takes any cloneable
  URL, so a self-hosted forge is already expressible. What an internal host
  actually needs is *host trust*, not a new transport — see the axis note below.
- **`s3` is a separate type rather than an `archive` with an `s3://` URL**
  because its auth model differs (SigV4 with a region and an explicit credential
  grant vs an anonymous HTTPS GET). Collapsing them would recreate exactly the
  "which fields apply?" ambiguity the union exists to remove. Note that needing
  credentials at all makes `s3` gated on §8's *per-repository credential grants*
  question — the tier never supplies them (§5).
- **`ref` MUST be an immutable commit id for official and delegated entries.** A
  branch name is a *mutable* pointer, so a signed index naming `main` signs
  nothing about the bytes: whoever can push to that branch changes what a
  "verified" app installs, and the app's `setup.onInstall` then runs unreviewed
  code. Signing the index is only meaningful if the index pins content. The
  publish pipeline (§2) **resolves the curator's branch/tag to a commit id at
  publish time** and emits that — so curators still author `main` and the
  published document always carries a pin. Updating an app is therefore an
  explicit republish, which is also what makes the catalog auditable. A mutable
  `ref` is tolerated only for untrusted user-external entries (where nothing is
  vouched for anyway) and local development.
- **Every variant must be content-pinned**, not just git: `sha256` for `archive`
  and `s3`, `digest` for `oci`, `integrity` for `bundle`. "Signed index pointing
  at unpinned content" is the same defect in each.
- **Unknown `type` fails closed.** An entry whose `source.type` the client
  doesn't recognize is dropped from the installable set — never partially
  handled. Because dropping an *entry* hides an app (unlike dropping an editorial
  section, which only hides a layout), the UI should surface "N apps require a
  newer KiroCrew" rather than silently shrinking the catalog.
- **Every new type costs a fetcher + a trust gate**, not just a schema variant.
  Adding one is a client change; the schema slot alone doesn't make it live.
- **The v1 JSON Schema must model `source` as a *closed* discriminated union** —
  `oneOf` on `type`, `additionalProperties: false` per variant, and each variant's
  pinning field (`sha256` / `digest` / `integrity`) marked `required`. Without
  that, "additive" is an unchecked assertion: a loosely-modelled `source` would
  happily validate an `archive` entry with no digest, i.e. an unpinned download.

**Legacy flat form stays accepted** as sugar, normalized in one place in the
reader, so the existing bundled file and the internal catalog keep working with
no migration and no schema-major bump:

```
{ gitUrl | repo, branch }  →  { source: { type: "git", url, ref } }
```

### 3.2 Three orthogonal axes — don't conflate them

| Axis | Question | Where it lives |
|------|----------|----------------|
| **Transport** | how are the bytes fetched? | `source.type` (§3.1) |
| **Host trust** | may we fetch from this host, and with whose credentials? | client-side trusted-host allowlist (public forges ∪ owner-configured registries ∪ edition-contributed internal hosts) |
| **Catalog trust** | who vouches for this entry? | trust tier (§5) |

An internal git farm or internal artifact store is a **host-trust** change (add
the host to the allowlist, decide the credential posture), not a transport
change. Keeping these separate is what stops a transport enum from quietly
implying "and it's safe to send credentials there."

### 3.3 Other internal registries are a different axis (delegation)

"Pull from another internal registry" is **not** a `source.type` — a registry
index is a catalog, not an app's bytes. It belongs at the **document** level as
delegation: the official registry names other indexes whose entries are folded
in, each with its own trust posture.

```jsonc
{
  "schemaVersion": 1,
  "generatedAt": "2026-07-29T19:00:00Z",
  "delegates": [
    {
      "name": "acme-internal",
      "url": "https://…/app-registry.json",
      "trust": "official",
      "publicKey": "…"        // REQUIRED for trust:"official" — see below
    }
  ],
  "apps": [ /* entries as above */ ]
}
```

This is deliberately a superset of today's user-configured external registries
(same fetch-and-merge machinery, add-only, dedupe by `name`), with one added
capability: a delegate reached *through the signed official doc* can be marked
`trust: "official"`, because the first-party curator vouched for it — whereas a
user-configured registry stays untrusted (§5). That is the mechanism by which an
internal edition ships an internal catalog without every app entry having to live
in the public file.

Hard limits on what delegation confers and costs:

- **The delegate's CONTENTS must be authenticated, not just its URL.** Naming a
  delegate inside the signed official document authenticates *that this URL was
  designated* — it says nothing about the bytes served there. A compromised
  delegate endpoint would otherwise have its unsigned catalog merged at Official
  trust, making attacker-chosen apps featurable and installable. So
  `trust: "official"` **requires** either the delegate's own signing key
  (`publicKey`, carried in and therefore vouched for by the signed root) or a
  pinned digest of an immutable delegate revision. A delegate that fails
  verification is dropped — it does **not** silently downgrade to untrusted,
  because a downgrade would let an attacker choose the tier by breaking the
  signature.
- **`trust: "official"` confers catalog trust only** — eligibility for featuring
  and editorial reference. It **never** confers clone-credential posture. A
  delegate's entries are third-party-authored, so they clone credential-free in a
  strict sandbox exactly like a user-external entry (§5). Ambient credentials for
  an internal source require an explicit per-repository grant, never a tier.
- **Delegated entries are subject to the same content-pinning rule** as official
  ones (§3.1): immutable commit / digest, no mutable refs.
- **Depth capped at one level** — a delegate's own `delegates[]` is ignored, so
  the merge cannot cycle.
- **Breadth and work capped too** — depth alone does not bound cost: the root doc
  could name an unbounded number of delegates. The schema caps `delegates[]`
  length, and the client fetches them with bounded concurrency and a total time
  budget, degrading to "delegates unavailable" rather than stalling a browse.

### 3.4 Baked search fields + curated categories

The "minimal index" rule above has one necessary exception, for the same reason
every comparable store has it: **you cannot search or list a catalog you have not
fetched.** Discover renders a dense sortable list, a category rail, and a search
box over *every* app before the user clicks anything. If display data lived only
in each app's `app.json`, the client would have to fetch N remote manifests just
to paint the first screen — today that means a throwaway shallow clone per app.

So the published document carries a small **denormalized search subset** per
entry, and the rule that keeps it honest is that it is **generated, never
authored**:

```jsonc
{
  "name": "oncall-radar",
  "source": { /* … */ },
  // ── generated at publish time from the app's own app.json ──
  "displayName": "Oncall Radar",
  "summary": "Surfaces your oncall pages in one place.",  // short, list-safe
  "author": "acme",
  "tags": ["ops", "oncall"],       // author-declared, free-form
  "version": "1.2.0",
  "iconRef": "…", "heroRef": "…"   // resolved media pointers
  // NOTE: `category` is NOT in this list — it is curator-assigned, not
  // manifest-derived. See the tags-vs-categories table below.
}
```

- **`category` is deliberately excluded from the manifest-derived set.** Every
  other baked field originates in author-controlled `app.json`; `category` is
  assigned by the curator in the catalog. If it were manifest-derived an author
  could self-promote into a curated category, which is exactly what the taxonomy
  is meant to prevent. Likewise, a **delegate's** per-entry `category` values and
  any `categories[]` it declares are **ignored** unless the signed root supplies
  an explicit override — taxonomy authority stays with the root curator.
- **Generated, not hand-maintained.** The publish pipeline (§2) already fetches
  and resolves each entry, so it bakes these from the app's own `app.json`. App
  authors still never edit the catalog — they edit their manifest, and the next
  publish picks it up. This preserves the single-source-of-truth premise while
  giving the client something searchable. Precedent: Obsidian's
  `community-plugins.json` carries `name`/`author`/`description` explicitly *for
  search*, and Homebrew serves a fully generated JSON API off its tap.
- **Advisory cache, not authority.** On the detail page the app's live manifest
  wins; the baked copy exists for list/search/first-paint. Long-form fields
  (screenshots, highlights, body) stay lazy and are never baked.
- **Security bonus.** Because the baked fields live *inside the signed document*,
  they are signed claims: a compromised app repo cannot silently change what the
  store displays until a republish. This is strictly better than rendering
  unsigned text fetched from an arbitrary repo at browse time.
- **Staleness is bounded and visible.** The baked copy is only as fresh as the
  last publish, which is the same cadence that already governs the pin in §3.1 —
  a version bump and a description change land together, by construction.

**Tags vs categories** are different things and must not be conflated:

| | Source | Vocabulary | Who assigns |
|---|---|---|---|
| `tags` | app's own `app.json` | free-form | the app author |
| `category` | curated taxonomy in the official doc | controlled, stable ids | the curator |

The taxonomy itself is document-level, which is what lets it change without a
client release — closing the hardcoded-category-taxonomy gap tracked in **issue
#581**, where the category list currently lives in frontend source:

```jsonc
"categories": [
  { "id": "ops", "label": "Ops", "order": 10 },
  { "id": "productivity", "label": "Productivity", "order": 20 }
]
```

- The editorial `category-order` section (§4) references these **ids**, not
  display strings, so relabeling or reordering is a catalog edit.
- Tolerant reader applies here too: an entry whose `category` id is not in
  `categories[]` falls into a default bucket and is **never dropped** — an
  unknown category must not hide an app.
- `tags` stay searchable/filterable but never define the rail, so an author
  cannot promote their app into a curated category by declaring a tag.

### 3.5 Document wrapper

The document wrapper carries the schema version and a generator stamp:

```jsonc
{
  "schemaVersion": 1,
  "generatedAt": "2026-07-29T19:00:00Z",
  "delegates": [ /* optional, §3.3 */ ],
  "categories": [ /* curated taxonomy, §3.4 */ ],
  "apps": [ /* entries */ ],
  "removed": [ /* tombstones, §3.6 */ ]
}
```

A bare top-level array (today's format) is still accepted and read as
`schemaVersion: 1, apps: <array>` so nothing breaks during rollout.

### 3.6 Tombstones (`removed`) — pulling an app must be positive information

An entry **disappearing** from the index is not a usable removal signal, and the
fail-safe ladder makes that worse rather than better:

- absence is ambiguous — pulled deliberately, or a truncated/failed fetch?
- the stale-cache fallback means a client that can't reach the CDN keeps serving
  the *old* index, so **a pulled app would keep showing indefinitely**;
- an already-installed app gets no signal at all, since install state is local.

So removal is carried as **explicit, positive** data that survives caching and
merge:

```jsonc
"removed": [
  {
    "name": "abandoned-app",
    "reason": "deprecated",        // "deprecated" | "superseded" | "withdrawn" | "malicious"
    "since": "2026-07-20",
    "note": "No longer maintained; use oncall-radar.",
    "replacedBy": "oncall-radar",  // optional
    "advice": "keep"               // "keep" | "disable" | "uninstall"
  }
]
```

Semantics:

- A tombstone **wins over an entry of the same `name` from any lower-or-equal
  tier**, including a stale cached one — this is what makes a pull effective
  without a client release.
- `reason: "malicious"` with `advice: "uninstall"` is the yank path: the app
  leaves Discover *and* the installed app is flagged with a prominent warning.
  Everything milder leaves installed copies working (`advice: "keep"`) and only
  removes the app from discovery.
- Tombstones are **persisted append-only on the client and never cleared by
  omission**. A newer valid document that simply *stops listing* a tombstone does
  NOT clear it — otherwise one document (or one truncated/rolled-back publish)
  silently resurrects a yanked app, including a `malicious` one. Un-removing an
  app requires an **explicit validated reinstatement record** naming it; only that
  deletes the persisted tombstone. A failed fetch obviously cannot resurrect
  anything either.
- **Tombstones and reinstatement records are permanently append-only.** A client
  can be offline arbitrarily long, so "keep it as long as some client might still
  hold the tombstone" has no finite bound — stating it as a *window* would let an
  implementation prune reinstatements and strand those clients suppressing an app
  forever. They are therefore never pruned. Growth is negligible (a handful of
  short records) and the alternative — a bounded supported-client age plus a
  checkpoint protocol for clients older than it — is real complexity to buy back
  bytes we do not need. If the list ever does need bounding, that checkpoint
  protocol is the mechanism, not silent pruning.
- Precedent: Obsidian keeps `community-plugins-removed.json` and
  `community-plugin-deprecation.json` as separate first-class documents rather
  than relying on absence.

`removed` lives **inline in the registry document** (§3.5) — one fetch, one atomic
view, so there is no way to hold a fresh index against a stale tombstone list.

---

## 4. Editorial schema (fail-safe + versioned)

`editorial.json` is a **presentation manifest**, decoupled from the raw index.
It only *references* apps by `name`; actual app data always resolves through the
registry. Editorial can therefore never inject a phantom or spoofed app — it can
only arrange apps that already exist and pass admission (this contains the same
class of featured-spoof vector already handled by the App Store's app-trust
checks).

```jsonc
{
  "schemaVersion": 1,
  "minClientVersion": "0.1.2",     // client below this ignores the doc entirely
  "generatedAt": "2026-07-29T19:00:00Z",
  "sections": [
    { "type": "spotlight", "appRef": "oncall-radar", "blurb": "…" },
    { "type": "rail", "title": "Made by the team", "appRefs": ["pptx-maker", "meetnote"] },
    { "type": "banner", "md": "New: **Channels**", "cta": { "label": "Learn more", "href": "…" } },
    { "type": "category-order", "order": ["productivity", "ops", "fun"] }  // category IDs, not labels
  ]
}
```

### Schema-evolution rules ("change how the page looks/works")

The design principle that makes layout changes safe without breaking old
clients is the **tolerant reader** with **additive-only** evolution:

1. **Data-driven sections.** The page is a `sections[]` array of typed objects,
   not a fixed template. New layouts = new `type` values.
2. **Skip-unknown.** The client renders `type`s it knows and **silently drops**
   unknown ones. A new `type: "carousel"` reaches new clients and is invisible
   (not broken) on old ones.
3. **Additive-only within a major.** New fields are optional; existing fields
   never change meaning. Bump `schemaVersion` **major** only for a breaking
   change; a major the client doesn't support triggers full fallback (§4
   ladder), not a partial render.
4. **`minClientVersion` gate.** Lets the server force older clients onto their
   bundled default when a doc relies on behavior they lack, independent of the
   schema major.
5. **Per-section validation, not all-or-nothing.** A section that fails
   validation (unknown required field, `appRef` that doesn't resolve, malformed
   `md`) is dropped individually; the rest of the page still renders.

The contract (allowed `type`s, required/optional fields per type, the tolerant-
reader + additive rules) is the JSON Schema in `KiroCrewApps` (co-located with
the data now, migrating to `KiroCrewAppSDK` later — §2), so
client, curator tooling, and validator share one definition.

### Fail-safe fallback ladder

Every layer is a *validated overlay*; the store never hard-fails:

```
live fetch (official editorial doc, CDN)
  → validated last-known-good disk cache      (stale > missing)
    → bundled default editorial snapshot        (compiled floor)
      → built-in client-side heuristic            (always renders something)
```

This reuses the **fetch-then-swap** refresh discipline the registry already
uses: the cache is overwritten only on a successful, validated fetch, so a
network blip degrades to "slightly stale," never "apps vanished." Validation on
**read** (not just fetch) guards a hand-tampered or older-build cache file,
mirroring the registry's existing cached-entry re-validation (name + path-safety
gates).

---

## 5. Trust tiers

The official registry needs a tier **between** bundled and user-external:

| Tier | Source | Featuring / editorial | Clone credentials | Authenticity |
|------|--------|-----------------------|-------------------|--------------|
| **Bundled** | compiled `app-registry.json` | honored | ambient (owner-designated) | ships in the wheel |
| **Official** *(new)* | KiroCrew-owned CDN doc | **honored** (only once the signature verifies) | **credential-free + strict sandbox** | host-pin **and** required detached signature |
| **Official delegate** *(new, §3.3)* | index named by the official doc | honored | **credential-free + strict sandbox** | **own signing key or pinned digest, verified** (§3.3) — being *named* in the signed root is not sufficient |
| **External (user)** | user-configured repos | ignored | credential-free + strict sandbox | none |

Two properties of this table are load-bearing and were nearly got wrong:

**Catalog trust never implies credential posture.** Only the *bundled* tier gets
ambient git/ssh credentials, because only bundled entries are
owner-designated-at-build-time. Every remotely-fetched entry — official,
delegated, or user-external — clones **credential-free in a strict sandbox**.
This matches what the code already decided:
`index_originated = bool(entry.get("_registry"))` in `install_from_registry`
forces the credential-free path precisely because an index entry can name a
private *sibling* repo on a host that is already trusted, and cloning it with the
gateway's identity would read that private repo as a confused deputy. An official
entry is index-authored by construction and a delegate's entries are
third-party-authored, so granting either ambient credentials would reopen exactly
that hole. If ambient credentials are ever needed for an internal source, they
require an explicit **per-repository** grant, never a tier-wide one.

**Host-pinning alone does not authenticate the bytes.** A fixed first-party
origin defeats a network attacker with no valid certificate for that hostname. It
does **not** defeat a DNS or CDN-origin takeover, or a compromised bucket — those
serve valid TLS from the pinned hostname, and the client would accept unsigned
documents that grant attacker entries Official featuring and point installs at
attacker-controlled code. Therefore a **detached signature** (minisign/cosign over
the doc bytes, rooted in an offline key shipped in the client — see below) is **required before Official
trust is granted**, not a fast-follow. An official document that fails signature
verification is treated as absent — the client falls through the §4 ladder to its
cached/bundled state rather than honoring unverified featuring.

**Key rotation must not re-couple yanking to an app release — but an OR-set of
keys is the wrong way to get there.** A naive "ship two keys, accept either"
scheme is strictly *worse* than one key: it grants the not-yet-active key signing
power immediately, so compromising **either** key is sufficient to forge a
catalog, and a static set carries no way to revoke. The model instead separates
the trust root from the signing key:

- The client ships an **offline root public key**. The corresponding private key
  is held offline, used rarely, and never touches publish CI.
- The root signs a small **key-metadata document** that designates **exactly one
  active document-signing key**, with an explicit activation time and expiry.
- The catalog and editorial documents are signed by the *active signing key*. A
  client accepts a document only if the signing key is the one the current,
  unexpired, root-signed metadata designates.
- **Rotation is a publish**: sign and publish new key metadata naming the new
  signing key. No app release. **Revocation is the same act** — superseding the
  metadata withdraws the old key, which the OR-set could not express.
- Only compromise of the **root** key requires a client update. Compromise of a
  signing key is recoverable in-band, which is what keeps the yank path decoupled
  from the release train.
- Metadata carries expiry so stale-but-validly-signed key metadata cannot be replayed
  forever; an expired metadata document is treated like a missing one (fall
  through the §4 ladder, grant no Official trust).

This is the standard root-vs-signing-key split (as in TUF and comparable update
frameworks); the operational details of custody and CI gating remain open in §8.

Relationship to `rfc-federated-app-platform` §5.2's tiers: its **Built-in** ≈
this RFC's Bundled, **Curated** ≈ Official, and **Local** ≈ a locally-installed
path (out of scope here). Its **Community** ("medium trust, hash-pinned") has *no*
equivalent here — this RFC's External (user) tier is **untrusted**, not
medium-trust, and the Official-delegate tier has no federated counterpart. The
two documents must not be read as using one shared tier vocabulary.

---

## 6. Client changes (KiroCrew)

- **One authenticated fetcher, one cache dir, one validator** for both docs,
  reusing the manifest-cache machinery (`_manifest_cache_dir()`, atomic writes,
  TTL, fetch-then-swap). Editorial and official-registry are two files under the
  same cache root. Signature verification (§5) gates acceptance.
- **Replace the `_registry` boolean with an explicit tier.** This is a
  prerequisite, not a detail. Today `_registry` is overloaded three ways: it is
  the featuring filter (`!a._registry` in `pickFeatured()`), the verified-badge
  rejection (`isVerified()` in `components/appstore/types.ts` returns `false` on
  any `_registry`, with tests locking that ordering in), **and** the backend's
  credential-posture key (`index_originated`). An official entry arrives through
  the same fetch-and-merge machinery, so it would carry `_registry` and land
  un-featurable *and* badged unverified — the exact opposite of §5 and §9. Fix:
  carry `_tier: 'bundled' | 'edition' | 'official' | 'official-delegate' |
  'external'`, and gate each consumer on the tier it actually cares about
  (featuring/badging on catalog trust; cloning on credential posture, which is
  ambient for `bundled` only). The existing tests that encode the boolean
  ordering must be updated deliberately, not incidentally.
- **Merge order** in `list_registry()`: bundled → **edition rows** → official →
  official delegates (§3.3) → external, each add-only over the previous (dedupe by
  `name`). The edition seam is not optional to name: `_load_registry_file()`
  already merges edition/CPP rows into the bundled list add-only, so live code has
  four sources and this ladder must say where a CDN document sits relative to an
  internal edition row. Decision: **an edition row wins over an official row of
  the same `name`** (an internal deployment's own catalog is more specific than the
  public one).
- **Tombstone suppression belongs in the shared entry-lookup path, not only in
  `list_registry()`.** `get_registry_app()` is a separate synchronous lookup over
  the bundled file + external caches and is what install-by-name resolves against;
  filtering only the list path would leave a `reason: "malicious"` app hidden from
  Discover yet still installable by name. Tombstones are applied after the full
  merge in *both* paths.
- **List/search reads the baked fields; detail reads the live manifest** (§3.4).
  The Discover list, search, and category rail render entirely from the published
  document — no per-app manifest fetch on browse. The lazy manifest fetch stays,
  but only for the detail view, where it supersedes the baked copy.
- **Categories come from the document, not from source** (§3.4), replacing the
  hardcoded taxonomy (issue #581). An entry whose `category` id is unknown falls
  into a default bucket and is never hidden.
- **Enforce the content pin at install time**, not just at publish: refuse to
  install an official or delegated entry whose `source` lacks an immutable pin
  (§3.1). The publish pipeline should make this unreachable; the client check is
  the backstop that makes it true regardless of who produced the document.
- **Verify each delegate before merging it** (§3.3) — signature or pinned digest.
  A delegate that fails verification is dropped, never downgraded to a lower tier.
- **Source dispatch**: one `switch` on `source.type` selecting a fetcher, with a
  fail-closed default for unknown types, plus the legacy flat→tagged
  normalization at the read boundary (belt-and-braces with the publish-time
  normalization in §2). Host trust stays a separate gate from transport (§3.2).
- **`pickFeatured()` becomes a fallback**, not the primary path: when a valid
  editorial doc is present, the Discover layout is driven by `sections[]`; when
  it's absent/invalid (bottom of the ladder), the current heuristic renders.
- **`refresh_registries()`** extended to refetch the official registry +
  editorial doc alongside configured external registries, with the same
  per-source `{ok, refreshed, failed}` reporting.
- **Installed-app reconciliation**: on refresh, cross-check installed apps
  against tombstones and surface the `advice` (warn / offer disable / offer
  uninstall). This is the only path by which an already-installed app learns it
  was pulled.
- **Bundled fallback generation**: a build step (or bot PR) pulls the current
  *published* document (§2) and writes `kiro_crew/apps/app-registry.json` as the
  compiled snapshot — including its tombstones, so an offline client still
  honors removals.

---

## 7. Rollout

1. **Contract + data + generated publish.** In `KiroCrewApps`, land the two JSON
   Schemas (co-located, versioned from `schemaVersion: 1`, `source` as a closed
   discriminated union with `git` only) and populate the authored catalog (seeded
   from the current bundled entries) + an initial `editorial.json`. Build the
   validate-normalize-stamp-**sign**-publish workflow (§2) as the *only* path to
   the CDN.
2. **Tier refactor (prerequisite).** Replace the overloaded `_registry` boolean
   with `_tier` and repoint featuring, verified-badging, and credential posture at
   it (§6), updating the tests that encode the old ordering. Doing this first is
   what makes step 3 land as designed instead of shipping official apps as
   unverified and un-featurable.
3. **Client (read path).** Official-tier fetch with **signature verification as
   the acceptance gate** + merge (incl. the edition seam) + tombstone application
   in the shared lookup path + validated caching + the fallback ladder. Source
   dispatch with fail-closed unknown types. `pickFeatured()` demoted to fallback.
   Bundled snapshot generated from the published document.
4. **Editorial-driven Discover.** Render `sections[]`; keep the heuristic as the
   floor. Ship the first curated layout.
5. **Schema → SDK migration (later).** Once published app-author tooling or the
   client consume the schema directly, move the schema files to
   `KiroCrewAppSDK`, publish them from there, and repoint the validator import.
   The wire `schemaVersion` is unchanged, so no published-doc consumer is
   affected.

---

## 8. Open questions

Cross-references to these use **names, not numbers**, so resolving one never
leaves a stale pointer elsewhere in the document.

1. **Editorial cadence & authoring UX** — hand-edited JSON in `KiroCrewApps`
   with schema validation in CI (proposed) vs a small authoring tool. Who
   curates, and how often?
2. **Snapshot-sync mechanism** — build-time generation vs scheduled bot PR into
   `KiroCrew`. Trade-off: build-time is always fresh but couples the KiroCrew
   build to a `KiroCrewApps` fetch; a bot PR keeps the build hermetic but can
   lag. *(This is why §2's table marks that row open rather than settled.)*
3. **Schema version scheme** — integer majors (proposed, matches the installed-
   app `schemaVersion: int`) vs semver on the schema. Integer is simpler for the
   skip-unknown/full-fallback split.
4. **Which transport lands second** — `archive`+`sha256` (simplest, and the
   natural fit for an internal artifact store) vs `s3` (SigV4, needs the
   credential-grant answer below) vs `oci`. v1 ships `git` only; the enum slot is
   reserved either way, and each one costs a fetcher + a host-trust decision, not
   just a schema variant.
5. **Signing key custody** — §5 answers the *structure* (an offline root key
   signing metadata that designates one active signing key, so rotation and
   revocation are both publishes rather than app releases). What remains is
   operational and needs a security owner: where the offline root private key
   lives and who can operate it, who is authorized to sign a publish, how signing
   is gated in CI, and what the metadata expiry interval should be.
6. **Per-repository credential grants** — §5 settles the tier question (no tier
   ever confers ambient credentials) but not the mechanism: if an internal git
   farm or S3 bucket genuinely needs authenticated reads, what does an explicit
   per-repository grant look like, who authors it, and is it owner-local config
   rather than anything a fetched index can influence?

**Resolved in-doc** (previously listed here; recorded so the decision isn't
relitigated):

- **Feed transport is HTTPS/CDN JSON**, not the git-clone pipeline. This is no
  longer optional: §2's immutable-per-revision publish, §5's host-pin +
  signature, and §6's single fetcher/cache all assume an HTTPS document. It fits
  the existing distribution CDN, is edge-cacheable, and needs no clone sandbox
  for the index. (Individual *apps* are still cloned per `source.type`.)
- **Signature is required in v1**, not a fast-follow, and no tier confers ambient
  clone credentials (§5).
- **Tombstones live inline** in the registry document (§3.6).

## 9. Success criteria

- Adding/removing/re-featuring an app is a `KiroCrewApps` PR that reaches
  clients within one cache TTL — **no app release**.
- A new editorial section `type` renders on new clients and is invisible (not
  broken) on old ones.
- With the CDN unreachable or the doc malformed, the Discover page still renders
  from cache → bundled snapshot → heuristic, with no blank state and no
  phantom/spoofed apps.
- The official registry's `featured` is honored; a user-external registry's is
  still ignored.
- **A tombstoned app disappears from Discover even on a client serving a stale
  cached index, and is not installable by name either.** An already-installed copy
  surfaces the removal advice. Neither a failed fetch nor a document that merely
  omits the tombstone resurrects it.
- **An unsigned or signature-failing official document grants no trust** — the
  client falls through to cache/bundled instead of honoring its featuring.
- **No remotely-fetched entry ever clones with ambient credentials**, regardless
  of tier — official, delegated, and user-external all clone credential-free in a
  strict sandbox.
- **Official apps are featurable and show as verified**, proving the `_tier`
  refactor actually landed (the `_registry` boolean would have made both false).
- **An invalid catalog cannot reach the CDN** — the publish workflow is the only
  write path and validates first.
- **No official or delegated entry can be installed from a mutable ref** — every
  such entry carries an immutable pin, enforced at publish AND at install.
- **A delegate whose contents fail verification contributes nothing** — it is
  dropped, not silently downgraded to a lower trust tier.
- **Discover renders and searches with zero per-app manifest fetches**, from the
  baked fields alone.
- **The category taxonomy changes without a client release** (closes #581), and an
  unknown category never hides an app.
- **Signing-key rotation AND revocation require no app release** — publishing new
  root-signed key metadata is sufficient, and the superseded key stops being
  accepted. Only root-key compromise needs a client update.
- Adding a second `source.type` requires no schema-major bump and no change to
  any existing entry.
