---
title: Projects — portable, syncable context bundles
status: in-progress
revision: 8
author: kseam
created: 2026-08-21
last-audited: 2026-09-02
audited-at: 928976d82
doc-pr: 4941
implementation-prs: []
tracking-issues: [3551]
supersedes: []
superseded-by: []
---

# RFC: Projects — portable, syncable context bundles

> **Implementation status (2026-09-01):** P0 and the first P1 vertical are
> active. The current branch has
> strict v1 manifest creation/validation, an atomic install-local registry,
> local and existing-checkout registration, managed Git clone plus
> fast-forward-only sync, `project create/add/list/show/sync`, and a portal for
> listing, inspecting, creating, adding, and syncing those same bundles under
> **Agent Capabilities → Projects**. The first agent-context vertical is also
> active: `context.agents`, `context.skills`, and `context.mcp` are inventoried
> while inert, then an explicit owner trust action materializes namespaced
> agents and skills, credential-free MCP definitions, and declared repo clones.
> Deactivation removes only that tracked materialization. Sync refreshes an
> already-active bundle only while its reviewed capability digest is unchanged;
> changed agents, skills, MCP, or repo declarations are withdrawn for explicit
> re-review. Sessions now persist a first-class `project_id`, derive
> their workspace from `self` or an owner-activated repo checkout, look up every
> declared repo from stable install-local state without cloning or fetching,
> receive a bounded first-turn Project brief containing the actionable repo paths, and can be created
> atomically from the Project view; that view lists live and historical attached
> sessions. A primary repo failure prevents attachment, while an unavailable
> secondary repo is reported in context without blocking the session. Healthy
> Projects are also available from the general new-session menu. Portal state
> has durable list/detail/create/add URLs, unavailable Projects expose
> recovery or removal. Removal preserves operator-owned folders while deleting
> only Crew-owned managed clones and derived state. Existing derived repo
> checkouts are reused without reopening them by name for Git operations and are
> disclosed as stale in the Project brief; removal and re-addition rebuild them.
> The trust review names every agent and shows exact MCP commands, arguments, and
> URLs before a separate confirmation. Manifest editing remains an external file
> workflow in v1 until the destination can be replaced with a true atomic
> compare-and-swap rather than a check-then-replace race.
> Bundle routes live under `/api/project-bundles`, preserving the existing
> task-runner `/api/projects` contract without adding an unused alias namespace.
> General source-provider bindings and
> pinned-doc indexing remain post-v1 work.

## TL;DR

* The context an agent needs to be useful — repos, a Jira board, Confluence
  spaces, ServiceNow records, a knowledge base — is scattered and install-bound.
  Starting the same work on a second machine means re-assembling all of it by
  hand.
* A **Project** is a local-first, declarative bundle of context sources. It is an
  ordinary directory containing a small credential-free `project.yaml` and
  optional pinned context. It works without Git and can later be shared by
  committing that same directory to a Git repository.
* Crew has no project user, organization, ownership, membership, ACL, or hosted
  registry model. Local filesystem permissions and Git credentials remain the
  authority. A bundle has a stable project identity; a person does not acquire a
  Crew identity by creating or cloning it.
* This is a **core primitive**, not a bookmark list. A Project answers *where
  the work items come from* and gives the agent the whole picture: sessions
  map to projects (many sessions per project) with a project view and
  one-click session creation from a project; Crew artifacts, the knowledge
  graph, steering, skills, MCP servers, agent crew, and workflows all scope to
  the project.
* v1 has one deliberately active source type: `repo`. Other typed entries may
  travel as inert inventory, but cannot be selected as the working repository
  or synchronized. A source-provider SPI
  is post-v1 work, introduced when a second source type proves the abstraction.
  The manifest's typed source entries leave room for that evolution without
  making an unimplemented extension seam part of the v1 contract.
* **Shareability is the load-bearing requirement.** A Project must be usable
  from any Crew instance and any workspace: clone it on a second machine, a
  teammate's install, or attach it from a different workspace on the same
  install, and the same picture materializes. Every feature in this document
  passes one test — *can it be expressed as synced bundle data plus locally
  rebuildable state?* — and anything that cannot is per-install state that
  degrades gracefully.
* Everything derived — managed clones, connector caches, knowledge-base indexes
  — is materialized locally per install and **never stored in the bundle**.
  Knowledge bases travel as *recipes*, not blobs. This deliberately mirrors the
  memory-architecture decision that nothing merge-hostile crosses installs.
* v1 is deliberately narrow: bundle identity and validation, local or managed-Git
  registration, `repo` materialization, one project per session, explicit
  agent/skill/MCP trust, and the minimal Projects UI. Pinned-doc indexing,
  knowledge, external connectors, artifact links, source bindings, and team
  ergonomics follow as independently shippable phases.

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

1. A first-class **Project**: a local-first directory bundle declaring repos,
   issue boards, doc spaces, observability views, ITSM records, knowledge bases,
   and pinned documents.
2. **The same bundle works locally or in Git**: create and use it without a
   remote, then optionally commit and share it without converting formats.
3. **No user identity system**: Crew delegates access to filesystem permissions
   and Git credentials and stores no project owner, organization, membership, or
   ACL records.
4. **Stable project and source identity**: an immutable project `id` survives a
   clone or rename; every source has an `id` used by recipes and local bindings.
5. **A small v1 source model**: `repo` is the only materialized source type.
   The manifest preserves other stable typed source ids as inert inventory so
   later provider work does not require changing project identity.
6. **Attachable to any session** — dashboard, Slack, cron, subagent — injecting
   a compact project brief and setting the slot's project directory from the
   bundle's resolved workspace source.
7. **Portable across Crew instances and workspaces** — the key requirement. A
   local bundle can be registered on one install; a Git-backed bundle can be
   cloned on another. Every install resolves sources against its own credentials,
   and a project belongs to no workspace.
8. **Searchable as one surface**: one federated search verb over local clones,
   built knowledge bases, and connector caches.
9. **Sessions map to projects** — many sessions per project and exactly one
   project per session in v1: a project view
   listing them, one-click session creation from a project, and auto-tagging
   of sessions to the project they are working.
10. **Project-scoped surfaces**: Crew artifacts, the knowledge graph, and the
   project's agent context (crew, skills, MCP servers, steering, workflows)
   all attach to the project, so any session on it inherits the same working
   picture.

## Non-goals

* **Syncing memory, lessons, or session history.** Those remain install-local.
  A Project describes *what the work is*, not *what the agent remembers about
  it*. This keeps the bundle free of everything merge-hostile (SQLite WAL,
  embedding stores, last-writer-wins preference files). *Project memory* — a
  project-scoped place for what the agents learn about the work — is the one
  place this line is genuinely tense; open question 3 names the options
  without breaking the rule.
* **Carrying credentials.** The manifest names credential *slots*; it never
  contains a secret.
* **Managing users or access.** Crew does not model users, owners, organizations,
  memberships, ACLs, invitations, or sharing links. A local bundle is governed by
  the filesystem; a Git-backed bundle is governed by its Git host and credentials.
* **Replacing workspaces.** Workspace stays "whose memory this install uses";
  Project is "what body of work the session is about". They are orthogonal.
* **A hosted registry.** Git already provides optional sharing, versioning, and
  permissions; discovery infrastructure can layer on later.
* **Non-Git storage backends in v1.** S3 and hosted storage are deferred until a
  concrete consumer demonstrates that local directories plus Git are insufficient.
* **Multiple projects on one session in v1.** The persisted relation permits many
  sessions to reference one project, but a session has at most one `project_id`.

## Design

A Project = a **bundle directory** (source of truth, small, human-editable) +
**materialized state** (derived, local, rebuildable, never stored in the bundle).

The bundle has one format and two origins:

1. **Local**: Crew creates or registers an ordinary directory. No Git repository
   or network access is required.
2. **Git-backed**: Crew clones a repository containing the same bundle format, or
   registers an existing local checkout. Git is transport, history, and access
   control; it is not the project's identity and Crew does not model the Git user.

Managed Git accepts local paths, scp-style remotes, and `file`, `git`, HTTP(S),
or SSH URLs. Remote-helper protocols and embedded HTTP credentials are rejected.
Malformed URL authorities are rejected as Project input errors rather than
escaping the owner API or CLI parser.
Local paths and decoded `file` URLs that resolve into Crew-sensitive locations
are rejected before Git is spawned. `file` URLs must be local and cannot name a
network authority. Relative repo paths declared by a bundle resolve against the
bundle root before both the sensitive-path decision and Git clone. Relative
add-command paths resolve against the caller's current directory. Control
characters are rejected again after URL decoding, so a percent-encoded NUL cannot
reach filesystem path resolution.
Clone and sync run a trusted Git executable inside Crew's standard subprocess
sandbox with resource limits and non-interactive prompting. The child environment
is credential-scrubbed; only allowlisted OS keychain helpers resolved to trusted
absolute executables and the exact trusted `gh auth git-credential` command may be
restored from system/global Git config. A missing trusted executable rejects the
helper rather than delegating its lookup to `PATH`.
Trusted absolute Git transport-helper paths are shell-quoted before Git appends
the repository argument, including Git for Windows installs below `Program Files`.
Managed Project Git commands enable Git for Windows' long-path support so the
derived state hierarchy and Git's temporary pack names do not hit `MAX_PATH`.
Bundle-relative paths are portable paths: POSIX-rooted and Windows drive-rooted
spellings are rejected on every host. Git transport helpers are also resolved
outside `PATH`; on Windows only their fixed Git for Windows installation roots
are admitted.
Repository-local execution hooks, transports, URL rewrites, worktree redirects,
and credential helpers are refused. Per-project/source locks serialize mutations,
and declared default branches are used for the initial clone. Managed bundle sync
fast-forwards its own configured branch. Declared
and Git-discovered branch names use the same validation, reject option-shaped
leading hyphens, and are never passed before Git's end-of-options marker.

A local bundle becomes shared by committing it to Git. No export, migration, or
manifest rewrite occurs.

### Manifest — `project.yaml`

Lives at the root of the bundle directory. Declarative and credential-free:

```yaml
apiVersion: crew.kiro/v1
kind: Project
id: 018f4f4a-760f-7a8b-a5d4-5a7e0f130d4e  # immutable; generated on create
name: payments-platform
description: The payments platform team's working context.

workspace:
  source: payments-api         # source id, or `self` when the bundle is the cwd

sources:                       # stable ids are referenced by recipes and bindings
  - id: payments-api
    type: repo                 # the only v1 source provider
    url: https://github.com/acme/payments-api
    default_branch: main
  - id: payments-infra
    type: repo
    url: https://github.com/acme/payments-infra

  - id: pay-board
    type: jira                 # post-v1 provider
    site: acme.atlassian.net
    board: PAY
    jql: "project = PAY AND statusCategory != Done"
    items: [PAY-1234, PAY-1290]   # optionally pin specific work items

  - id: pay-linear
    type: linear
    workspace: acme
    team: PAY

  - id: pay-notion
    type: notion
    workspace: acme
    databases: [payments-decisions]

  - id: pay-confluence
    type: confluence
    site: acme.atlassian.net
    spaces: [PAYDOCS]
    pages: []                  # optional pinned page IDs

  - id: pay-datadog
    type: datadog
    site: datadoghq.com
    monitors: ["team:payments"]
    dashboards: [payments-golden-signals]

  - id: pay-servicenow
    type: servicenow
    instance: acme.service-now.com
    tables:
      - name: cmdb_ci_service
        query: "name=payments-platform"

knowledge:                     # recipes composed over the sources above
  - id: payments-runbooks      # a named, reproducible knowledge base
    build:                     # how any install rebuilds it locally
      from:
        - source: payments-api
          paths: ["docs/**/*.md"]
        - source: pay-confluence
          space: PAYDOCS

context:                       # the project's agent context
  steering: [steering/*.md]    # optional, carried in the bundle
  skills: [skills/]            # optional, per-install trust required
  mcp: mcp.json                # optional MCP servers, per-install trust required
  agents: [agents/*.json]      # agent specs, namespaced when activated
  workflows: [workflows/]      # named workflow definitions for this project
  pinned:                      # docs carried verbatim in the bundle
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

* **Stable identity, no user identity.** `id` is generated once and survives a
  display-name change, directory move, or Git clone. It contains no owner or
  namespace. Two registrations with the same `id` are two materializations of the
  same logical project; two different ids with the same name are distinct projects.
* **The manifest is bounded external input.** Loading and validation read
  `project.yaml` through an opened-handle, no-follow containment check. The v1
  portal does not replace an existing manifest: the available filesystem
  primitives cannot atomically refuse a destination changed by another process.
  Operators edit the ordinary bundle file directly and then sync or re-add it.
  Oversized or excessively nested YAML is rejected, and provider config
  shares one depth/node expansion budget so aliases cannot amplify a small file
  into an unbounded JSON tree.
* **The bundle may be the working repository.** `workspace.source: self` makes the
  bundle root the session working directory, whether or not that directory is a Git
  repository. Otherwise `workspace.source` names one declared source id. This avoids
  forcing a small project to maintain a second context repository while supporting
  bundles that span many source repositories.
* **One working directory, many usable repositories.** A session keeps one primary
  cwd because existing file and shell tools require one, but attachment resolves every
  declared `repo` source. The bounded Project brief gives the agent each source id and
  absolute checkout path. The primary source is fail-closed; a missing secondary is
  shown as unavailable and does not prevent work in the remaining repositories.
  Attachment tries registered bundle paths newest-first; a path that becomes
  unresolvable or develops a link loop is treated as unavailable so an older healthy
  registration can still attach, and no usable registration yields the normal bounded
  Project error instead of escaping as a filesystem exception.
* **Source references do not duplicate configuration.** Recipes, links, and local
  bindings name a source `id`; they do not repeat its URL or provider config.
* **No secrets, ever.** Each install resolves credential slots from its own
  store. A missing credential degrades gracefully: the source is listed as
  *unavailable* in project health, not silently absent.
* **Knowledge bases are recipes, not blobs.** `knowledge` entries declare how
  to *build* the KB from underlying sources. Every install materializes its own
  index through the existing Knowledge Library pipeline. The definition
  travels; the index is rebuilt. No embedding-store merge problem.
* **Pinned docs travel in the repo.** Small load-bearing documents are
  committed alongside the manifest — versioned with it, available offline.

### Source providers — post-v1 extension

Every `sources` entry names a `type`; v1 implements only `repo` directly. Other
types remain inert inventory and cannot be selected as `workspace.source`. A
provider SPI is intentionally deferred until a second active source type can
validate its shape. The expected post-v1 operations are:

```
validate(entry)        -> config errors before any network call
sync(entry, cache_dir) -> refresh the local snapshot (TTL-aware)
search(entry, query)   -> hits with provenance, from cache or live API
health(entry)          -> available | unavailable(reason) | degraded
credential_slots(entry)-> the named slots this entry resolves (possibly empty)
```

Expected provider tiers:

1. **Built-in**: `repo` (Git clone/fetch) is the v1 behavior and
   becomes the reference adapter if the post-v1 SPI lands.
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

In v1, a manifest naming any source type other than `repo` preserves that entry
as inventory but does not sync or search it. A post-v1 provider design should
degrade a missing installed provider to *unavailable* without breaking unrelated
sources, but that behavior is not part of the v1 contract.

### Materialized state — local only

Per install, state is keyed by immutable project id rather than display name:

```
~/.kiro/crew/
  trust/project-registry/
    registry.json                # protected project id -> local registrations
  projects/
    managed/<project-id>/bundle/ # Crew-managed Git clone, when applicable
    state/<project-id>/
      sources/<source-id>/       # managed repo-provider clones
      cache/<source-id>/         # connector snapshots
      knowledge/                 # built KB indexes
      state.json                 # sync, health, credentials, recipe hashes
```

An externally managed local bundle remains at the path the operator registered;
Crew does not copy or take ownership of it. A Git URL without an existing checkout
is cloned into `managed/<project-id>/bundle/`. Everything under `state/` is
rebuildable and never enters the bundle.
Unregistering a Project removes that exact Project's Crew-owned `state/` tree and
managed bundle clone after capability withdrawal. It never removes a local bundle
or an existing checkout registered from outside the data home.

The registry is install-local authority: it decides which bundle path later
receives owner-approved activation and session attachment. It therefore lives
under the keystone-protected `trust/` root, is owner-only, rejects links and
oversized input, rejects an oversized replacement before publishing it, and
revalidates every stored path against Crew-sensitive locations when loaded. The
registry id remains authoritative if an externally edited manifest presents a
different project id: the bundle is shown as unavailable under its registered id,
so list-driven actions cannot be redirected to another registered Project.
The mutable clones and derived state remain under
`projects/`, where ordinary Project work can use them without gaining a way to
rewrite registry authority. Before Crew creates directories or invokes Git in
that derived tree, it rejects links and junctions at the target and every ancestor
below the Project-state root, so a planted checkout alias cannot redirect a fetch
or fast-forward into another working tree.

Post-v1 local source bindings are also per-install state. A binding maps a logical source
id to an existing checkout, allowing a session to use the operator's branch,
worktree, or dirty tree instead of a managed clone. Removing a binding falls back
to provider-managed materialization.

### Bundle origins

| Origin | Registration | Update behavior | Authority |
|---|---|---|---|
| Local directory | `crew project add <path>` | Read current files in place | Filesystem permissions |
| Managed Git clone | `crew project add <git-url>` | Fetch and fast-forward the configured branch | Git host + local credentials |
| Existing Git checkout | `crew project add <path>` | Read current checkout; operator owns Git operations | Filesystem + existing Git config |

`crew project create <path>` creates a local bundle and writes its identity. Making
it shareable is an ordinary Git operation performed in that directory. `sync` never
pushes and never invents a Git identity; edits remain ordinary working-tree changes
that the operator or an existing agent workflow may commit and propose through the
repository's normal process.

### Sharing model — instances and workspaces

Shareability is a requirement, not an emergent property, and it has three
distinct dimensions:

1. **Across instances**: a local bundle is available wherever its directory is
   available. A Git-backed bundle is cloned with `crew project add <url>` on the
   second install and materializes the same picture — sources, pinned docs,
   knowledge recipes, and agent context behind that install's trust grant.
2. **Across workspaces on one install**: a project belongs to **no
   workspace**. Materialized state lives at the install level
   (`~/.kiro/crew/projects/state/<project-id>/`), so two workspaces attaching the same
   project share one clone set, one cache, one built knowledge graph — no
   duplication, no divergence. The workspace keeps owning memory and
   preferences; the project supplies the subject-of-work to whichever
   workspace's sessions attach it.
There is deliberately no third Crew-level dimension for people. Sharing with
another person means granting them filesystem or Git access outside Crew. The
bundle contains no owner or member record, and Crew does not infer identity from
Git author configuration or credentials.

The corollary is the **bundle test** used throughout this document: every
project feature must be expressible as *synced bundle data + locally
rebuildable derived state + per-install state that degrades gracefully*. A
feature that requires state to flow between installs outside the bundle
(merging caches, syncing memory, copying indexes) fails the test and is
redesigned until it passes — that is why knowledge travels as recipes and
artifact links travel as a `links.yaml` index while artifact blobs stay local.

### Sessions belong to projects

Sessions map to projects **many-to-one** in v1, and the mapping is a first-class
`project_id` field, not an inference from the directory path. The existing
`slot.project` path is the session's derived working directory, not the identity
of the Project bundle:

* **Attach**: a session binds a project by id (selected through its name in a
  dashboard picker or CLI, or by asking to "work on payments-platform").
  Attachment resolves the primary workspace first, then looks up the remaining
  owner-activated repo checkouts concurrently with a bounded worker pool while preserving
  manifest order in the result. It never clones or fetches: a mutable bundle manifest
  cannot turn session restore into a new outbound request. It injects a compact **project brief** — name, primary
  workspace, absolute repo paths or unavailable status, other source inventory,
  description, and source inventory — not the full content.
  Actionable paths precede prose so a long description cannot crowd them out of the
  bounded prompt context. Failure of `workspace.source` aborts attachment atomically;
  failure of any secondary repo degrades only that repo.
  An existing derived checkout is an offline-capable cache. Attachment reuses it,
  marks that source stale in the brief, and performs no clone, fetch, or by-name Git
  read or write after validation. Repository materialization happens only during the
  explicit owner-reviewed activation flow; removal and re-addition are the v1
  refresh/reset operation after a source URL changes. Failed activation staging clones
  use read-only-aware tree removal so Git object modes cannot strand derived state on
  Windows. Both Project-brief
  boundary markers are removed from benign bundle-authored text before the trusted
  background-reference wrapper is added. Credentials and suspicious exfiltration
  URLs are redacted from the assembled brief before it enters model context. A
  brief matching the shared prompt-injection screen is dropped entirely and
  SEL-audited instead of entering the model prompt.
  Resolved workspace paths containing control characters are rejected before the
  raw path can enter the trusted session preamble, and a restored `project_id` must
  be a canonical UUID rather than merely a non-empty metadata string. Restore
  re-resolves the current workspace and brief before binding an agent session;
  failure aborts rather than running one turn in a stale persisted checkout.
* **Dashboard integration points**: two, both deliberately boring. (1) A
  **Projects tab under Agent Capabilities** — bundle management stays grouped
  with the agent context and tools it supplies, and selecting one opens the
  project view. (2) The
  **new-session flow gets a project picker**: today starting a session asks
  for a directory; picking a *Project* instead pre-fills the directory from
  the resolved workspace source and brings the Project brief with it. Activated
  agents, skills, and MCP entries are install-level capabilities available to
  any session; v1 does not claim that those global registries are session-scoped.
  The directory-only path stays for work that has no project.
  An attached session's composer identifies the bundle by its Project display
  name, not by the repository currently supplying its working directory. The
  folder-and-branch label remains only for directory-scoped sessions.
  The first portal slice lives at `/capabilities?tab=projects`; the existing
  Task Runner keeps `/projects`, with `/tasks` as its compatibility redirect.
  Task-plan links carrying the legacy `?applied=` query resolve to Task Runner
  so an in-flight plan does not open the bundle manager. **New session** on a
  Project detail creates an already-attached
  session, and the sidebar's general create menu lists every healthy registered
  Project. Unavailable Projects stay visible for recovery but cannot start a
  session.
* **Project view**: the dashboard gets a per-project view — its sessions
  (live and historical), work items, artifacts, source health, last sync.
  This is *the* answer to "what is happening on payments-platform".
* **Create session from project**: one click on the project view opens a new
  session already attached — project dir set from the resolved workspace source, brief
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
* **Relation to today's project directory**: attaching resolves
  `workspace.source` to a working directory. `self` resolves to the registered
  bundle root; a source id resolves through managed repo materialization. That path populates the existing
  `slot.project` compatibility field, so file search, `@`-mentions, and
  project-scoped `.kiro/` behavior keep working while new code uses `project_id`
  for bundle identity. Every other repo source is materialized beside it in
  install-local Project state and is reachable through the absolute path carried in
  the brief; changing the primary source is not required to inspect or edit it.
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
  index (`links.yaml`) in the bundle, so it syncs; the artifact content itself
  stays in the local library unless deliberately pinned into the bundle.
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

All of it is declarative config in the bundle; everything executable or
credential-adjacent sits behind the per-install trust grant.

The implemented v1 activation slice uses `context.agents`, `context.skills`, and
`context.mcp`. Other context keys are rejected rather than silently ignored; v1 has
no legacy `crew` alias to preserve.
Every path or glob is bundle-relative and link-confined. Before trust, the
Projects tab shows counts, agent names, and each MCP server's exact command and
arguments or URL, but installs nothing. **Trust and activate** requires a separate
confirmation over that review and is an owner-only, SEL-audited install decision
that:

* clones each `type: repo` source into derived state under the Project id;
* writes agents under a `project--<project-id>--` filename namespace and gives
  each one a `project-<project-id>-` runtime name;
* copies skills beneath `skills/projects/<project-id>/`; and
* merges MCP servers into the install-owned MCP source with the same runtime
  namespace.

The review token binds the canonical bundle location to a digest of every
activation-relevant declaration and byte: repo source configuration, agent JSON,
skill trees, MCP JSON, and the context paths selecting them. Activation refuses a
token if any of that content changed after the owner reviewed it. Agent and MCP
validation, digesting, and installation use one retained bounded byte snapshot per
file, preventing a path swap between those stages from installing unreviewed bytes.
Project agents cannot declare hooks or `file:` prompts/resources: those fields execute
commands or make kiro-cli read install-local files outside Crew's sensitive-path gate.
They also cannot embed `mcpServers`; executable MCP declarations must use
`context.mcp`, where the owner review exposes each exact command, argument, or URL.
Inline prompts and non-file resources remain valid portable data. Before status reports
an activation as active, Crew verifies that every tracked agent, skill tree, and MCP entry
still exists; a deleted output fails closed. Full content verification remains on repeated
activation, refresh, deactivation, and removal.
A managed bundle sync compares the newly fetched digest with the activation record;
a change withdraws the tracked agents, skills, and MCP entries and leaves the Project
inactive until the owner reviews and activates the new content. If malformed synced
content prevents digest computation, sync still withdraws the tracked activation
before returning the error. Bundle-only prose or display-name changes do not expand
the trust grant. This security invalidation
removes locally modified namespaced agents and skills plus MCP entries that still
carry Project provenance; an MCP entry whose provenance marker was removed remains
an explicitly reclaimed user entry. Ordinary deactivation still refuses to delete
locally modified outputs.
Executable resolution, digest validation, installation, deactivation, and
unregistering serialize on the same per-Project lock. Revocation or
removal therefore cannot complete while an earlier activation still has a path to
publish capabilities.
The critical revocation audit is appended inside that lock before any tracked output
or registration is removed; an audit failure leaves both intact. Removal commits the
registry deletion before deleting Crew-owned managed storage, so a failed registry
write cannot destroy the bundle while leaving its registration reachable. An
unchanged-digest refresh materializes all repositories before updating its activation
record and keeps the prior agents, skills, MCP entries, and record if repository
refresh fails.
Repeating explicit activation for the already-active bundle with the same review key
verifies its tracked outputs and returns them unchanged; only sync performs a repository
refresh for an unchanged grant.
Every owner-only Project route also requires its allowed permission event to be
recorded; an unavailable SEL refuses the operation instead of creating an unaudited
authorization gap. Project attachment applies that same fail-closed audit before
looking up already-materialized repositories and before the slot is created. Project-domain
exception text is credential- and suspicious-URL-redacted before it reaches dashboard
JSON, diagnostic logs, or persisted and broadcast session error rows.

Bundle-owned agent and MCP JSON is read through descriptor-pinned, no-follow
file descriptors and must be a single-link regular file. Platforms without
POSIX descriptor-relative opens use a handle-validated hardened read that checks
the opened file's containment and hardlink status. Manifest source/context lists,
capability matches, tree scans, JSON bytes, and staged skill entries/bytes all
have fixed limits; recursive context globs are rejected. Installed skill digest
verification uses the same bounded, descriptor-pinned posture before removal.
Install-owned destination ancestors for Git locks, managed bundle clones, derived
repo checkouts, and Project skill staging are rejected when any existing component
is a link or junction, with another check immediately before publication.
The bundle cannot carry
credentials, `allowedTools`, `toolsSettings`, or MCP `autoApprove`.
Remote MCP definitions must be credential-free HTTP(S) URLs; stdio definitions
may contain only `command` plus string `args`. Deactivation reads the tracked
activation record from the keystone-protected trust directory and removes only
the matching namespaced outputs. The record must name the requested Project, and
every recorded agent, skill, MCP, and repository output must remain inside that
Project's exact namespace; copied or cross-Project state is rejected before any
removal. Standalone MCP entries carry Project-id
provenance through the install source and rendered agent config, so withdrawal
also removes only matching servers and tool references; a locally reclaimed
entry without matching provenance is preserved. An existing but unreadable or
version-invalid activation record aborts deactivation and Project removal rather
than treating installed outputs as absent and orphaning them. Repo clones are
derived cache while the Project remains registered; Project removal deletes the
exact Crew-owned derived state and managed clone after capability withdrawal.
These activated entries are
namespaced but install-global in v1.
The rendered-config writer re-reads the current Project MCP source inside its
final commit lock. If any Project-owned entry changed, it discards the stale
render and retries once from the new source; an overlapping rebuild therefore
cannot restore a revoked or superseded server. An entry whose marker was locally
removed keeps its current configuration and loses only Crew's provenance claim.
Project attachment itself scopes the working repositories and Project brief;
strict per-session capability scoping is a later capability-registry concern.

### Sync model

* `crew project sync <name-or-id>` refreshes the registered bundle first. A
  managed Git bundle fetches and fast-forwards its configured branch; an
  externally managed local directory is read in place and never mutated by
  sync. An active Project is refreshed automatically only when its content-bound
  capability review token is unchanged. Existing derived source checkouts are
  retained as stale caches rather than refreshed by name. A changed agent, skill, MCP definition,
  context selector, or repo declaration withdraws the old materialization and
  requires explicit owner review before activation. An unchanged-token refresh keeps
  the prior activation usable unless every declared repository materializes
  successfully. Later provider phases may refresh non-Git caches past TTL and
  rebuild KBs whose recipe hash changed (hashes in `state.json`).
* Auto-sync via a script cron per project (no LLM): pull + refresh on an
  interval; failures surface in project health, not chat noise.
* Edits to the project are ordinary bundle file edits. If the bundle is in Git,
  agents may propose manifest edits through the repository's existing diff/PR
  workflow. Crew neither commits nor pushes during sync and never configures a
  Git author identity.
* **No derived-state sync, by design.** Two installs never merge indexes or
  caches; each rebuilds from the same recipe.

## Migration plan

v1 is P0 plus P1. Later phases extend the same bundle and stable source identity
without changing persisted project identity.

* **P0 — bundle kernel.** Versioned manifest validation; immutable project and
  source ids; `crew project create/add/list/show/sync`; local-directory,
  existing-checkout, and managed-Git registrations; direct `repo` source
  handling; `workspace.source: self` and declared-source resolution. No session or UI
  behavior changes in this phase.
  *Exit criteria:* a bundle created without Git validates and lists; committing
  the same directory to Git requires no conversion; adding its Git URL on a
  second install preserves the project id; two bundles with the same display
  name and different ids coexist; sync never mutates an externally managed
  checkout and never commits or pushes.
* **P1 — session attachment and minimal Projects UI.** Add `project_id` to the
  session record while retaining the derived `slot.project` working-directory
  compatibility field; compact project-brief injection; one Project per session;
  a focused project view (sessions + sources + health) in the Projects tab under
  Agent Capabilities; project picker and create-session-from-project flow.
  Bundle management (single-column list/detail, create/add/sync, source
  declarations, local materializations, and manifest health) is
  already available at
  `/capabilities?tab=projects`; this phase completes the session-specific half of
  the view. The Project-detail creation path, attached-session list, and healthy
  Project choices in the general new-session menu are implemented. The same tab
  inventories and explicitly activates bundled
  agents, skills, repo sources, and MCP definitions. Activation installs
  namespaced capabilities for the install; attachment does not pretend those
  shared registries are isolated to one session.
  Manifest editing stays in the operator's file editor in v1; Git commits and
  pushes remain external. The portal editor is deferred until a destination change
  can be refused atomically across processes. Registering the same Project id from a new
  primary path uses the same capability-aware gate and is refused while activation
  state remains.
  *Exit criteria:* local and Git-backed projects both attach; `self` and a declared
  primary source select the correct working directory; every declared repo receives
  a stable checkout path in session context; an unavailable secondary repo is visible
  without blocking attachment, while an unavailable primary repo fails atomically;
  the project view lists attached sessions across workspaces without merging their
  memory; a session created from the project arrives with its id, brief, and working
  directory; an install that never registers a bundle behaves unchanged.
* **P2 — knowledge recipes + federated search.** `knowledge.build`
  materialization through the existing Knowledge Library pipeline into a
  project-scoped partition (the project knowledge graph); recipe-hash
  invalidation; federated search over clones + KBs. *Exit criteria:* deleting
  the local KB index and running sync reproduces search results; changing a
  recipe triggers a rebuild on next sync and only then; graph/semantic queries
  scope to the project by default.
* **P3 — first-party providers.** Add Jira, Linear, Notion, Confluence, Datadog,
  ServiceNow, forge work items — including the normalized work-item shape,
  item-level `items:` scoping, and session-from-work-item. Credential slots +
  health surfacing. *Exit criteria:* a missing credential or
  missing provider shows the source as unavailable without failing attach;
  search returns board/page/monitor/record hits with provenance; a new
  provider can be added without touching the sync engine or manifest schema
  (proven by adding the last first-party provider against a frozen engine).
  Blocked on open question 1 (third-party provider packaging).
* **P4 — project surfaces + team ergonomics.** Project artifacts (attach,
  artifacts-as-input, `links.yaml` reference mapping), auto-tagging of
  sessions, steering/workflow activation beyond the shipped agents/skills/MCP
  trust slice,
  agent-proposed manifest PRs, project templates, full dashboard project page.
  *Exit criteria:* an artifact links to a work item and the link survives a
  fresh clone on a second install; an untrusted project's mcp/crew/workflows
  are listed-but-inert until granted; a session started in a project clone
  gets a suggested tag.

Each phase is independently shippable and independently abandonable.

## Backward compatibility

`slot.project` keeps accepting a bare directory path; a Project attachment is an
additional way to derive and populate it. New persisted state uses `project_id` for
bundle identity and calls the resolved path a working directory, avoiding further
overload of the legacy field name. Workspaces, the Knowledge Library, and
project-scoped `.kiro/` config are consumed as-is. An install that never registers a
project bundle sees no new behavior.

## Security considerations

* **Credential-free bundle**: cloning someone's project grants no access —
  every source resolves against the local install's own credential store.
  Sharing a project shares a map, not keys. Crew stores no user, owner, member,
  organization, or ACL record; the filesystem or Git host remains authoritative.
* **Trust gate on executable content**: `context.skills`, `context.steering`,
  `context.mcp`, `context.agents`, and `context.workflows` carried in a project
  repo are third-party executable(-adjacent) content. They pass through the
  same per-directory trust grant as project-scoped skills (the #3551
  machinery): listed-but-marked until the user trusts the project checkout —
  an untrusted project's MCP servers are never started and its workflows never
  run. Pinned docs (inert text) load without a gate. The shipped
  agents/skills/MCP slice stores its activation record under the
  keystone-protected per-install trust directory, requires the reviewed
  canonical bundle path to round-trip, namespaces every global output, rejects
  links that escape or obscure the reviewed tree, and refuses portable
  credentials or tool-approval fields.
* **Prompt-injection posture**: synced Jira/Confluence/ServiceNow content is
  untrusted data, same as any web fetch — it feeds search results and context,
  never instructions.
* **Audit**: project attach/sync/trust events are SEL-audited like skills
  trust. Human CLI create/add/sync operations are audited too. The
  `kirocrew project` CLI group is a deliberate MCP-first exception: registration,
  cloning, and trust-boundary lifecycle actions remain operator-only and are not
  exposed as model-callable MCP tools.

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
4. **A Crew-hosted registry service.** Deferred: Git gives optional sharing,
   versioning, and permissions with zero new Crew infrastructure.
5. **S3 as a v1 bundle origin.** Rejected for v1: local directories and Git cover
   offline, versioned, and shared bundles without introducing a second conflict and
   authentication model. A future RFC can add another origin without changing the
   bundle format.
6. **Project identity derived from name, path, or Git remote.** Rejected: all three
   are mutable, and local bundles may have no remote. An immutable id stored in the
   manifest gives sessions, caches, trust grants, and clones one durable key without
   introducing user identity.

## Open questions

1. **Source-provider SPI and packaging** (blocks P3): when a second source type
   is ready, what is the smallest provider contract proven by both it and
   `repo`? Third-party providers could ship as Kiro Crew app contributions, as
   a generic `mcp` provider, or both. App-contributed providers get full
   caching/search federation but need a trust story; the `mcp` provider is
   weaker but needs no new packaging surface.
2. **Write-back scope**: may agents write to sources (transition a Jira
   ticket, attach a linked artifact to it) through the project binding, or is
   the project strictly a read/context surface with writes staying on today's
   per-tool paths?
3. **Project memory**: sessions on a project accumulate knowledge about the
   work. Three options that keep the no-merge-hostile-sync rule intact:
   (a) an install-local, project-scoped memory partition (doesn't travel);
   (b) curated *project notes* promoted into the manifest repo as pinned docs
   — durable decisions sync, mediated by git like any manifest edit;
   (c) both — working memory local, promotion to repo notes as the durable
   path. Recommendation is (c); the open part is whether promotion is manual,
   agent-proposed-as-PR, or automatic.
4. **Auto-tagging confidence**: what evidence suffices for a suggested tag
   (dir-inside-clone is strong; a single work-item mention is weak), and does
   a suggestion ever auto-confirm for unattended sessions (cron, webhook)?
