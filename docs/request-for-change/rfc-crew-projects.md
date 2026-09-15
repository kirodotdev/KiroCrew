---
title: Projects — portable, syncable context bundles
status: draft
author: kseam
created: 2026-08-21
last-audited: 2026-08-21
audited-at: 5cd92ff99
doc-pr: 4941
implementation-prs: []
tracking-issues: [3551]
supersedes: []
superseded-by: []
---

# RFC: Projects — portable, syncable context bundles

## TL;DR

* The context an agent needs to be useful — repos, a Jira board, Confluence
  spaces, ServiceNow records, a knowledge base — is scattered and install-bound.
  Starting the same work on a second machine means re-assembling all of it by
  hand.
* A **Project** is a named, declarative bundle of context sources: a small
  credential-free manifest stored in a Git repo (or S3 prefix), attached to any
  session by name, synced across Crew installs by plain `git pull`, and
  searchable as one federated surface.
* This is a **core primitive**, not a bookmark list. A Project answers *where
  the work items come from* and gives the agent the whole picture: sessions
  map to projects (many sessions per project) with a project view and
  one-click session creation from a project; Crew artifacts, the knowledge
  graph, steering, skills, MCP servers, agent crew, and workflows all scope to
  the project.
* Source types are **pluggable**: every entry in the manifest is handled by a
  registered *source provider* behind one SPI (validate / sync / search /
  health). `repo` is built in; Jira, Confluence, Datadog, ServiceNow, and
  anything else ship as providers — adding a source type never changes the
  manifest format or the sync engine.
* **Shareability is the load-bearing requirement.** A Project must be usable
  from any Crew instance and any workspace: clone it on a second machine, a
  teammate's install, or attach it from a different workspace on the same
  install, and the same picture materializes. Every feature in this document
  passes one test — *can it be expressed as synced bundle data plus locally
  rebuildable state?* — and anything that cannot is per-install state that
  degrades gracefully.
* Everything derived — clones, connector caches, knowledge-base indexes — is
  materialized locally per install and **never synced**. Knowledge bases travel
  as *recipes*, not blobs. This deliberately mirrors the memory-architecture
  decision that nothing merge-hostile crosses installs.
* Four phases, each independently shippable: P0 manifest + repos + pinned docs,
  P1 knowledge recipes + federated search, P2 Jira/Confluence/ServiceNow
  connectors + S3 backend, P3 team ergonomics.

## Motivation

Verified at `5cd92ff99`:

* A session's "project" today is one field: a local directory path on one slot
  (`slot.project`). It scopes file search, `@`-mentions, and — since the
  project-scoped `.kiro/` work — steering and skills. It does not travel and
  names nothing beyond a filesystem location.
* **Workspaces** (`workspaces: Record<string,{dir}>` in config) scope *memory
  and files on one install*. Two crews sharing a workspace share memory. Nothing
  about a workspace syncs, and the concept deliberately conflates "who I am /
  what I remember" with "what I am working on".
* The **Knowledge Library** is per-workspace and per-install. Its pipeline
  (chunk → entity/relation extraction → graph + FTS5 + optional embeddings)
  produces local derived state with no export, no import, and no way to
  reproduce the same library on a second install short of re-adding every
  source by hand.
* There is no Jira, Confluence, or ServiceNow reader anywhere in
  `src/kiro_crew` (grep for either term returns zero hits), and no manifest
  format that names external context sources.
* **Sessions have no grouping concept.** `slot.project` ties one session to
  one directory, but nothing groups the many sessions working the same body of
  work, no view lists them together, and artifacts, knowledge, and memory have
  no project scope at all.
* **Work items are invisible to Crew.** The tickets and issues a session
  exists to advance live in Jira/Linear/GitHub/GitLab; nothing in Crew names
  where a session's work comes from, so the agent never sees the whole
  picture — it sees one directory and one conversation.

The consequence: bringing context into a new session is manual, and bringing it
to a new *install* (a second machine, a teammate) is manual times every source.

## Goals

1. A first-class **Project**: a named bundle declaring repos, issue boards, doc
   spaces, observability views, ITSM records, knowledge bases, and pinned
   documents.
2. **Pluggable source types**: one provider SPI behind every source entry, so
   Datadog (or anything after it) is a provider registration, not an engine
   change.
3. Stored in **Git (or S3)** — versioned, diffable, shareable through the
   permission model the team already has.
4. **Attachable to any session** — dashboard, Slack, cron, subagent — injecting
   a compact project brief and setting the slot's project directory from the
   bundle's primary repo.
5. **Shareable across Crew instances and workspaces** — the key requirement.
   Synced across installs by cloning/pulling the manifest, with every install
   resolving the same sources against its own credentials; attachable from any
   workspace on an install, because a project belongs to no workspace.
6. **Searchable as one surface**: one federated search verb over local clones,
   built knowledge bases, and connector caches.
7. **Sessions map to projects** — many sessions per project: a project view
   listing them, one-click session creation from a project, and auto-tagging
   of sessions to the project they are working.
8. **Project-scoped surfaces**: Crew artifacts, the knowledge graph, and the
   project's agent context (crew, skills, MCP servers, steering, workflows)
   all attach to the project, so any session on it inherits the same working
   picture.

## Non-goals

* **Syncing memory, lessons, or session history.** Those remain install-local.
  A Project describes *what the work is*, not *what the agent remembers about
  it*. This keeps the bundle free of everything merge-hostile (SQLite WAL,
  embedding stores, last-writer-wins preference files). *Project memory* — a
  project-scoped place for what the agents learn about the work — is the one
  place this line is genuinely tense; open question 6 names the options
  without breaking the rule.
* **Carrying credentials.** The manifest names credential *slots*; it never
  contains a secret.
* **Replacing workspaces.** Workspace stays "whose memory this install uses";
  Project is "what body of work the session is about". They are orthogonal.
* **A hosted registry.** Git/S3 already provide sharing, versioning, and
  permissions; discovery infrastructure can layer on later.

## Design

A Project = a **manifest** (source of truth, small, human-editable) +
**materialized state** (derived, local, rebuildable, never synced).

### Manifest — `project.yaml`

Lives at the root of the project's git repo (or S3 prefix). Declarative and
credential-free:

```yaml
apiVersion: crew.kiro/v1
kind: Project
name: payments-platform
description: The payments platform team's working context.

sources:                       # each entry is handled by the provider named in `type`
  - type: repo                 # built-in provider
    url: https://github.com/acme/payments-api
    default_branch: main
    role: primary              # primary | reference
  - type: repo
    url: https://github.com/acme/payments-infra
    role: reference

  - type: jira
    site: acme.atlassian.net
    board: PAY
    jql: "project = PAY AND statusCategory != Done"
    items: [PAY-1234, PAY-1290]   # optionally pin specific work items

  - type: linear
    workspace: acme
    team: PAY

  - type: notion
    workspace: acme
    databases: [payments-decisions]

  - type: confluence
    site: acme.atlassian.net
    spaces: [PAYDOCS]
    pages: []                  # optional pinned page IDs

  - type: datadog
    site: datadoghq.com
    monitors: ["team:payments"]
    dashboards: [payments-golden-signals]

  - type: servicenow
    instance: acme.service-now.com
    tables:
      - name: cmdb_ci_service
        query: "name=payments-platform"

knowledge:                     # recipes composed over the sources above
  - id: payments-runbooks      # a named, reproducible knowledge base
    build:                     # how any install rebuilds it locally
      from:
        - source: repo
          url: https://github.com/acme/payments-api
          paths: ["docs/**/*.md"]
        - source: confluence
          site: acme.atlassian.net
          space: PAYDOCS

context:                       # the project's agent context
  steering: [steering/*.md]    # optional, carried in the project repo
  skills: [skills/]            # optional, per-install trust required
  mcp: mcp.json                # optional MCP servers, per-install trust required
  crew: [crew/*.json]          # agent specs for this project's crew
  workflows: [workflows/]      # named workflow definitions for this project
  pinned:                      # docs carried verbatim in the repo
    - docs/architecture.md
    - docs/oncall.md

credentials:                   # NAMES only — resolved per install
  required:
    - github
    - atlassian
  optional:
    - datadog
    - servicenow
```

Key properties:

* **No secrets, ever.** Each install resolves credential slots from its own
  store. A missing credential degrades gracefully: the source is listed as
  *unavailable* in project health, not silently absent.
* **Knowledge bases are recipes, not blobs.** `knowledge` entries declare how
  to *build* the KB from underlying sources. Every install materializes its own
  index through the existing Knowledge Library pipeline. The definition
  travels; the index is rebuilt. No embedding-store merge problem.
* **Pinned docs travel in the repo.** Small load-bearing documents are
  committed alongside the manifest — versioned with it, available offline.

### Source providers — the pluggable seam

Every `sources` entry names a `type`, and each type is a registered **source
provider** implementing one SPI:

```
validate(entry)        -> config errors before any network call
sync(entry, cache_dir) -> refresh the local snapshot (TTL-aware)
search(entry, query)   -> hits with provenance, from cache or live API
health(entry)          -> available | unavailable(reason) | degraded
credential_slot()      -> the named slot this provider resolves (e.g. "datadog")
```

Provider tiers:

1. **Built-in**: `repo` (git clone/fetch) ships with the sync engine — it is
   the only provider P0 needs.
2. **First-party providers**: Jira, Linear, Notion, Confluence, Datadog,
   ServiceNow, and forge work items (GitHub/GitLab issues and PRs, distinct
   from the `repo` clone provider) — shipped with Crew but structurally
   identical to any other provider; none is special to the engine. Board-level
   entries (a Jira "space", a Linear team) and item-level pins (one ticket)
   are both just provider config — `items:` narrows scope, it does not change
   the shape.
3. **Third-party providers**: a Kiro Crew app can contribute a provider the same
   way apps contribute skills and crons today, and a generic `mcp` provider
   wraps any MCP server as a source (weaker caching/search federation, but an
   escape hatch for long-tail systems).

A manifest naming a `type` with no locally-registered provider degrades the
same way a missing credential does: the source is listed as *unavailable
(provider not installed)* in project health — attach never fails on it, and
nothing else in the bundle is affected. The manifest format and sync engine
never change when a source type is added; that is the point of the seam.

### Materialized state — local only

Per install, under `~/.kiro/crew/projects/<name>/`:

```
manifest/          # the cloned project repo (or S3 sync)
clones/            # the repo provider's shallow clones (or links to existing local clones)
cache/<type>/      # one snapshot dir per source provider (jira/, confluence/, datadog/, ...)
knowledge/         # built KB indexes (chunks, FTS, embeddings)
state.json         # last sync time, per-source health, credential status, recipe hashes
```

Everything except `manifest/` is rebuildable and never syncs. The manifest
syncs by plain `git pull`/`git push` (or `aws s3 sync` with versioning for the
S3 backend).

### Storage backends

|  | Git (default) | S3 |
|---|---|---|
| Sync | clone/pull/push | `s3 sync` + bucket versioning |
| History | full, diffable | object versions (coarser) |
| Sharing | repo permissions | bucket policy |
| Conflicts | git merge (manifest is small; rare, human-resolvable) | last-writer-wins per object |
| Offline | yes | needs cached copy |

Git is primary: manifests are exactly the small, human-reviewed text git is
for. S3 serves environments where a git remote is awkward. Backend is
per-project: `crew project add <git-url>` vs `crew project add s3://…`.

### Sharing model — instances, workspaces, people

Shareability is a requirement, not an emergent property, and it has three
distinct dimensions:

1. **Across instances** (the same person on two machines, or a fresh install):
   `crew project add <url>` on the second instance materializes the same
   picture — sources, pinned docs, knowledge (rebuilt from recipes), agent
   context (behind that install's own trust grant). Nothing needs exporting
   from the first instance because nothing authoritative lives outside the
   bundle.
2. **Across workspaces on one install**: a project belongs to **no
   workspace**. Materialized state lives at the install level
   (`~/.kiro/crew/projects/<name>/`), so two workspaces attaching the same
   project share one clone set, one cache, one built knowledge graph — no
   duplication, no divergence. The workspace keeps owning memory and
   preferences; the project supplies the subject-of-work to whichever
   workspace's sessions attach it.
3. **Across people** (a team): sharing a project is sharing its repo — the
   permission model is the forge's. A teammate with read access gets the full
   picture; write access is the ability to propose or land manifest changes.
   Nothing personal leaks, because nothing personal is in the bundle:
   credentials are slots, memory is out of scope, and per-install state
   (session mappings, auto-tags, trust grants) never syncs.

The corollary is the **bundle test** used throughout this document: every
project feature must be expressible as *synced bundle data + locally
rebuildable derived state + per-install state that degrades gracefully*. A
feature that requires state to flow between installs outside the bundle
(merging caches, syncing memory, copying indexes) fails the test and is
redesigned until it passes — that is why knowledge travels as recipes and
artifact links travel as a `links.yaml` index while artifact blobs stay local.

### Sessions belong to projects

Sessions map to projects **many-to-one**, and the mapping is a first-class
field, not an inference from the directory path:

* **Attach**: a session binds a project by name (slot field, dashboard picker,
  or "work on payments-platform"). Attaching injects a compact **project
  brief** — name, description, source list with health, work-item summary,
  pinned-doc index — not the full content.
* **Dashboard integration points**: two, both deliberately boring. (1) A
  **Projects entry in the left rail** — browsing projects is a sibling of
  browsing sessions, and selecting one opens the project view. (2) The
  **new-session flow gets a project picker**: today starting a session asks
  for a directory; picking a *Project* instead pre-fills the directory from
  the primary repo and brings the brief, crew, steering, and skills with it.
  The directory-only path stays for work that has no project.
* **Project view**: the dashboard gets a per-project view — its sessions
  (live and historical), work items, artifacts, source health, last sync.
  This is *the* answer to "what is happening on payments-platform".
* **Create session from project**: one click on the project view opens a new
  session already attached — project dir set from the primary repo, brief
  injected, crew/steering/skills loaded. Optionally seeded from a work item
  ("start a session on PAY-1234") so the task arrives with its ticket.
* **Auto-tagging**: existing and incoming sessions are tagged to a project by
  evidence — project dir inside a project clone, a mentioned work item or PR
  that belongs to a project's sources — surfaced as a suggested tag the user
  confirms, never a silent reassignment. Tags make the project view complete
  without requiring discipline at session start.
* **Search**: one federated search verb over the project — clones (ripgrep),
  built KBs (FTS + vector), connector caches — with live API fallback when the
  cache misses and the credential is present. Results carry their source.
* **Relation to today's project directory**: the per-slot project dir becomes
  *derived* — attaching a Project with a `primary` repo sets the slot's project
  dir to that repo's local clone. Existing behavior (file search, `@`-mentions,
  project-scoped `.kiro/` steering and skills) keeps working unchanged.
* **Relation to workspaces**: unchanged. A workspace attaches many projects
  over time; a project is used from many workspaces and installs.

### Work items — where the work comes from

Work-item sources (Jira, Linear, forge issues/PRs) are ordinary providers, but
their content is treated as more than searchable text: the sync engine
normalizes items into a small common shape (id, title, state, assignee, url,
source) held in the provider cache, so the project brief can say "12 open
items, 3 assigned here", the project view can list them, and a session can be
created *from* one. Scope is whatever the manifest declares — a whole board, a
JQL slice, or pinned individual items via `items:`.

### Project artifacts

Crew artifacts (the artifact library) gain a project scope:

* **Attach**: an artifact produced in a project-attached session is tagged to
  the project by default; any artifact can be attached manually. The project
  view lists them.
* **Artifacts as input**: attaching a project makes its artifacts referenceable
  in-session ("use the payments dashboard artifact") the same way pinned docs
  are — part of the whole picture, not just output.
* **Reference mapping to external items**: an artifact can be *linked* to a
  work item (this mockup belongs to PAY-1234). The mapping is a lightweight
  index (`links.yaml`) in the project repo, so it syncs; the artifact content
  itself stays in the local library unless deliberately pinned into the repo.
  Whether Crew also writes the link back to the external item (an attachment
  or comment on the Jira ticket) is an open question (write-back scope).

### Project knowledge graph

The Knowledge Library pipeline (chunk → entity/relation extraction → graph +
FTS + embeddings) already builds a graph; `knowledge` recipes make that graph
**project-scoped**: each install materializes the project's KBs into a
partition keyed by the project, so graph queries, entity lookups, and semantic
search answer *within the project* by default. The graph is derived state —
rebuilt from recipes, never synced.

### Project agent context — crew, skills, MCP, steering, workflows

`context` in the manifest carries the project's *agent configuration*, so a
session created from the project starts with the right working setup, on every
install:

* **steering** — project steering files, loaded like `.kiro/steering` today.
* **skills** — project skills, per-install directory trust required.
* **mcp** — MCP servers the project's work needs, per-install trust required
  (same gate as skills: listed-but-marked until trusted, never auto-started).
* **crew** — agent specs for the project's crew, so "the payments crew" is
  reproducible from the bundle.
* **workflows** — named workflow definitions for recurring project processes,
  runnable from the project view or by name in-session.

All of it is declarative config in the project repo; everything executable or
credential-adjacent sits behind the per-install trust grant.

### Sync model

* `crew project sync <name>` = pull manifest, then refresh sources: fetch
  clones, refresh caches past TTL, rebuild KBs whose recipe hash changed
  (hashes in `state.json`).
* Auto-sync via a script cron per project (no LLM): pull + refresh on an
  interval; failures surface in project health, not chat noise.
* Edits to the project are edits to `project.yaml`, pushed like any
  config-as-code. Agents propose manifest edits as diffs/PRs against the
  project repo rather than writing directly.
* **No derived-state sync, by design.** Two installs never merge indexes or
  caches; each rebuilds from the same recipe.

## Migration plan

* **P0 — manifest + repos + pinned docs + session mapping.** `crew project
  add/list/sync`, git backend, attach-to-session, project brief injection,
  primary-repo → slot project dir, the session→project field, a minimal
  project view (sessions + sources + health) behind a Projects entry in the
  left rail, and a project picker in the new-session flow
  (create-session-from-project).
  *Exit criteria:* a project cloned on a second install attaches to a session
  and sets the same project dir contents; the brief lists every declared
  source; a manifest edit pushed from install A is visible on install B after
  one sync; the project view lists every attached session and a session
  created from it arrives attached; a second workspace on the same install
  attaches the project without re-cloning or rebuilding anything.
* **P1 — knowledge recipes + federated search.** `knowledge.build`
  materialization through the existing Knowledge Library pipeline into a
  project-scoped partition (the project knowledge graph); recipe-hash
  invalidation; federated search over clones + KBs. *Exit criteria:* deleting
  the local KB index and running sync reproduces search results; changing a
  recipe triggers a rebuild on next sync and only then; graph/semantic queries
  scope to the project by default.
* **P2 — provider SPI + first-party providers.** The source-provider SPI
  (validate/sync/search/health/credential_slot), provider registry, and
  first-party providers: Jira, Linear, Notion, Confluence, Datadog,
  ServiceNow, forge work items — including the normalized work-item shape,
  item-level `items:` scoping, and session-from-work-item. Credential slots +
  health surfacing; S3 backend. *Exit criteria:* a missing credential or
  missing provider shows the source as unavailable without failing attach;
  search returns board/page/monitor/record hits with provenance; a new
  provider can be added without touching the sync engine or manifest schema
  (proven by adding the last first-party provider against a frozen engine);
  the same project round-trips through an S3 prefix. Blocked on open question
  1 (third-party provider packaging).
* **P3 — project surfaces + team ergonomics.** Project artifacts (attach,
  artifacts-as-input, `links.yaml` reference mapping), auto-tagging of
  sessions, project agent context (crew/mcp/workflows behind the trust gate),
  agent-proposed manifest PRs, project templates, full dashboard project page.
  *Exit criteria:* an artifact links to a work item and the link survives a
  fresh clone on a second install; an untrusted project's mcp/crew/workflows
  are listed-but-inert until granted; a session started in a project clone
  gets a suggested tag.

Each phase is independently shippable and independently abandonable.

## Backward compatibility

Nothing existing changes shape. `slot.project` keeps accepting a bare directory
path; a Project attachment is an additional way to populate it. Workspaces,
the Knowledge Library, and project-scoped `.kiro/` config are consumed as-is.
An install that never creates a project sees no new behavior.

## Security considerations

* **Credential-free bundle**: cloning someone's project grants no access —
  every source resolves against the local install's own credential store.
  Sharing a project shares a map, not keys.
* **Trust gate on executable content**: `context.skills`, `context.steering`,
  `context.mcp`, `context.crew`, and `context.workflows` carried in a project
  repo are third-party executable(-adjacent) content. They pass through the
  same per-directory trust grant as project-scoped skills (the #3551
  machinery): listed-but-marked until the user trusts the project checkout —
  an untrusted project's MCP servers are never started and its workflows never
  run. Pinned docs (inert text) load without a gate.
* **Prompt-injection posture**: synced Jira/Confluence/ServiceNow content is
  untrusted data, same as any web fetch — it feeds search results and context,
  never instructions.
* **Audit**: project attach/sync/trust events are SEL-audited like skills
  trust.

## Alternatives considered

1. **Extend workspaces to sync.** Rejected: workspaces hold memory and
   history, and syncing those reintroduces the merge problem deliberately
   designed out of the remote-workspace architecture. Projects stay memory-free
   precisely so they can sync trivially.
2. **Sync built knowledge bases (blobs) instead of recipes.** Rejected:
   embedding indexes are large, version-coupled to the local model, and
   merge-hostile. Recipes are tiny and deterministic enough.
3. **Per-source bookmarks with no bundle.** Rejected: the value is the bundle —
   one name that brings repos + board + docs + KB together, portable as a unit.
4. **A Crew-hosted registry service.** Deferred: git/S3 give sharing,
   versioning, and permissions for free with zero new infrastructure.

## Open questions

1. **Third-party provider packaging** (blocks P2): sources are pluggable
   behind the provider SPI — decided. What remains open is how *third-party*
   providers ship: as Kiro Crew app contributions (like skills/crons today), as
   the generic `mcp` provider only, or both. App-contributed providers get
   full caching/search federation but need a trust story; the `mcp` provider
   is weaker but needs no new packaging surface.
2. **Name identity**: is `name` unique per install with the remote URL as the
   real identity, or globally scoped like `org/name`?
3. **Multi-project sessions**: can a session attach two projects (a platform +
   a service), and if so how do primary-repo and search scoping compose?
4. **Write-back scope**: may agents write to sources (transition a Jira
   ticket, attach a linked artifact to it) through the project binding, or is
   the project strictly a read/context surface with writes staying on today's
   per-tool paths?
5. **S3 backend priority**: is S3 needed at P2, or does git cover every real
   consumer for now?
6. **Project memory**: sessions on a project accumulate knowledge about the
   work. Three options that keep the no-merge-hostile-sync rule intact:
   (a) an install-local, project-scoped memory partition (doesn't travel);
   (b) curated *project notes* promoted into the manifest repo as pinned docs
   — durable decisions sync, mediated by git like any manifest edit;
   (c) both — working memory local, promotion to repo notes as the durable
   path. Recommendation is (c); the open part is whether promotion is manual,
   agent-proposed-as-PR, or automatic.
7. **Auto-tagging confidence**: what evidence suffices for a suggested tag
   (dir-inside-clone is strong; a single work-item mention is weak), and does
   a suggestion ever auto-confirm for unattended sessions (cron, webhook)?
