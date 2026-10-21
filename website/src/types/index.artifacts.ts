// Shared types for the artifact library: artifacts and their publication,
// publishing providers, forks, remote listings, session docs, folders, events,
// comments and web-app metadata. Consumers import them from `types/index.ts`.

export interface ArtifactPublication {
  /** Publishing-provider artifact UUID — stable across versions. */
  artifact_id: string
  /** Stable view URL: https://.../artifact/<id>. */
  view_url: string
  /** Publishing provider name (registry key of the destination). */
  provider?: string
  /** Sync authority: 'mirror' (authoritative in Kiro Crew) | 'live' (remote CRDT). */
  collab_mode?: 'mirror' | 'live'
  visibility: 'PRIVATE' | 'SHARED' | 'PUBLIC'
  shared_with: string[]
  auto_sync: boolean
  last_synced_kirocrew_version: number
  /** Maps Kiro Crew version (as string) -> provider version number. */
  version_map: Record<string, number>
  published_at: string
  published_by: string
  /** Conflict / sync-failure message surfaced to the UI; empty when healthy. */
  last_error: string
  /** Publish SUCCEEDED but the link is not usable yet (e.g. CloudFront still
   *  rolling out). NOT an error — rendered as a neutral/warn line beside the
   *  link, never in the danger surface `last_error` drives. Empty when the link
   *  is already reachable. */
  notice: string
  /** Machine discriminator for `notice`, so the UI can pick the right copy
   *  instead of assuming every notice is "still rolling out". One of
   *  `"rolling_out"` | `"distribution_disabled"` | `"unknown"`, or empty when
   *  there is no notice. The frontend selects its string from this and falls
   *  back to a generic (no time promise) line for an unrecognised value.
   *  Optional so an older gateway that omits it reads as "". */
  notice_code?: string
}

/** A publishing provider's self-described capabilities for a given artifact kind,
 *  returned by GET /api/artifacts/publish-providers?kind=<kind>. */
export interface PublishProviderDescriptor {
  name: string
  display_name: string
  capabilities: string[]
  kind_support: 'native' | 'converted' | 'degraded' | 'unsupported'
  capable: boolean
  /** False => tooling not installed yet; installs automatically on first publish.
   *  Optional: older gateways omit it (treat as available). */
  available?: boolean
  /** The provider's own remedy text for `available: false` -- which action makes it
   *  available. Optional: older gateways omit it, and a row with no hint simply shows
   *  none rather than inventing one. */
  install_hint?: string
  /** False => the published link requires authentication (content is stored privately),
   *  so the publish flow shows neither the public-exposure warning nor the "publish
   *  publicly" acknowledgment. Optional: older gateways omit it, and absence means
   *  reachable -- a missing warning on a public link is the worse mistake. */
  public_reachable?: boolean
  sharing_model: {
    supports_private: boolean
    supports_shared: boolean
    supports_public: boolean
    principal_kind: string
    supports_roles: boolean
    supports_expiration: boolean
    programmable: boolean
    out_of_band_url: string
  }
  sync_model: { authority: string; concurrency: string; collab_mode: 'mirror' | 'live' }
  discovery_model: {
    list_mine: boolean
    list_shared_with_me: boolean
    list_public: boolean
    full_text_search: boolean
    pull_by_id: boolean
  }
}

export interface ForkMetadata {
  upstream_artifact_id: string
  upstream_url: string
  upstream_owner: string
  upstream_version: number
  forked_at: string
}

/** One provider-neutral remote listing row, as returned by
 * GET /api/remote-artifacts/{provider}/browse. Inert in the public edition —
 * the endpoint 404s until a companion registers a provider.
 *
 * Fields fall into two groups:
 *  - The documented backend RemoteListing core (external_id, title, owner,
 *    view_url, updated_at, snippet) plus the handler's local_slug annotation —
 *    always present on a contract-faithful provider.
 *  - Optional companion extensions (tags, visibility, current_version,
 *    editable) beyond the base RemoteListing contract. A provider that only
 *    serializes the core dataclass omits these, so any UI gated on them (e.g.
 *    the Clone shortcut, which requires `editable`) simply doesn't render. */
export interface RemoteArtifact {
  /** The provider's stable id (clone/fork routes key on it). */
  external_id: string
  title: string
  owner?: string
  /** The provider's human link for the remote copy. */
  view_url?: string
  /** Best-effort ISO/epoch string for sort/display. */
  updated_at?: string
  snippet?: string
  /** Companion extension (not in the base RemoteListing contract). */
  tags?: string[]
  /** Companion extension (not in the base RemoteListing contract). */
  visibility?: string
  /** Companion extension (not in the base RemoteListing contract). */
  current_version?: number
  /** Set by the browse handler: the local slug if this remote artifact is
   * already cloned/forked onto this device (else null). */
  local_slug?: string | null
  /** Companion extension (not in the base RemoteListing contract).
   * Positive-only hint: the caller can edit the remote copy, so a Clone
   * shortcut is offered. NOT an enforcement gate — the provider remains
   * sole authority at push time. A core-only provider omits it, so Clone is
   * not shown (only Fork). */
  editable?: boolean
}

export interface Artifact {
  slug: string
  name: string
  /** `dashboard` is a CREWMATE'S DASHBOARD PACKAGE -- the `bound_to`, `model`,
   *  `view` and `theme` document `artifact_store/dashboard_package.py` validates.
   *  It is in this union because the Artifacts library lists and versions it like
   *  any other record, which is where its history and its revert come from, and
   *  deliberately NOT in any picker: the backend keeps it out of
   *  `USER_SELECTABLE_KINDS` so nobody can mint one by hand, and a kind offered in
   *  the UI that the server refuses is a dead menu entry. */
  kind: 'widget' | 'html' | 'markdown' | 'svg' | 'json' | 'text' | 'webapp' | 'image' | 'dashboard'
  /** Provenance/origin bucket. Carries either a legacy bucket
   * (chat|cron|subagent|manual|import) or the actual session origin
   * (dashboard|slack|cli|task-runner|unknown), so treated as an open string. */
  source: string
  /** Originating chat session key (for the Source column's title resolution). */
  session_key?: string
  /** Live-resolved title of the originating chat session (or "(deleted session)"
   * when that session is gone). Absent for non-chat origins — fall back to `source`. */
  session_title?: string
  description: string
  tags: string[]
  version: number
  created_at: string
  updated_at: string
  content?: string
  /** Opaque optimistic-concurrency token for `content`; echo it back as
   * `expected_token` on a content save. Absent for live file-backed artifacts. */
  content_token?: string
  /** Original source path for file-backed artifacts (live pointer). */
  source_path?: string
  /** True when the live state differs from the latest numbered snapshot.
   * Computed at GET time — accounts for both silent saves and external
   * file edits to source_path. Drives the "Snapshot Live" button. */
  live_dirty?: boolean
  /** Publication state. Absent/null until the artifact has been
   * published to a sharing provider. */
  publication?: ArtifactPublication | null
  /** Fork provenance (P1). Absent/null if not a fork. */
  fork_metadata?: ForkMetadata | null
  /** Short, markdown-stripped, redacted content preview — only present when the
   * list was requested with ?snippet=1 (used by the command palette's
   * Artifacts provider). For a ?content=1 query it is match-centered. */
  snippet?: string
  /** Library folder this artifact is filed in ("" / absent = unfiled/root).
   * Opaque folder id — resolve names via the artifact-folders list. */
  folder_id?: string
  /** User pin/favorite mark. Metadata-only (no version bump). Drives the
   * All | Pinned filter on the Artifacts page. */
  pinned?: boolean
  /** True when the store created this record itself from a chat-emitted
   * `<mcwidget>` rather than from an explicit save. Serialized straight off the
   * backend dataclass field of the same name (`Artifact.to_dict` is an
   * `asdict`, so every list/detail response already carried it — only this type
   * was missing it). Load-bearing for the chat Artifacts panel: the store
   * sweeps auto-registered records oldest-first past
   * `MAX_AUTO_WIDGET_ARTIFACTS` (200) unless `pinned`, so an
   * auto-registered-and-unpinned artifact is the ONLY one whose survival a
   * "save permanently" action changes. Absent on older payloads — treat
   * undefined as false. */
  auto_registered?: boolean
  /** True for a crewmate's weekly report or monthly review, set by the gateway
   * when `save_goal_report` files it (never through the artifact API). The
   * viewer opens such a page in the crewmate Dashboard page's sandbox
   * (lib/artifactFrame.ts). Absent on older payloads — treat undefined as false. */
  goal_report?: boolean
  /** Metadata for kind="webapp" artifacts (deploy state, architecture, costs). */
  webapp_metadata?: WebAppMetadata
  /** Metadata for kind="image" artifacts. The bytes themselves are never inlined
   * here — they are streamed from `/api/artifacts/<slug>/asset` with the
   * server setting Content-Type. Every field is optional because older payloads
   * and minimal saves may omit it, so every consumer must degrade gracefully:
   * `alt` gives the accessible description, `width`/`height` let the UI reserve
   * the correct aspect ratio before the image loads, and the rest are
   * informational (shown in details, used to name a download). */
  image?: {
    mime: string
    ext: string
    size_bytes?: number
    width?: number
    height?: number
    sha256?: string
    original_filename?: string
    alt?: string
  }
}

/** A non-code document produced during a chat session — the virtual entries
 * shown in the Artifacts "All" tab. Not a persisted artifact until saved
 * (materialized) via api.materializeArtifact(path). */
export interface SessionDoc {
  path: string
  name: string
  updated_at: string
  session_key: string
  /** Human-readable session title (falls back to the session key). */
  session_title: string
  message_ts: string
  /** True when this path already backs a saved (pinned) artifact. */
  saved: boolean
  /** Slug of the backing artifact when saved; empty otherwise. */
  slug: string
}

/**
 * A folder in the local artifact library. Nested via `parent_id`
 * (`""`/absent = root). Structurally compatible with `ChatFolder` so the
 * shared folder utilities (`orderFoldersWithPaths`, `computeReorderedFolders`,
 * `FolderMoveSubmenu`) work on both without adaptation.
 */
export interface ArtifactFolder {
  id: string
  name: string
  order: number
  parent_id?: string
  icon?: string
  /** Optional #rrggbb display color chosen by the user. */
  color?: string
  /** Direct artifact count (excludes subfolders) — computed server-side per GET. */
  item_count?: number
  /** Full ancestry path root→leaf ("Parent › Child") — computed server-side. */
  path?: string
}

export interface ArtifactEvent {
  ts: string
  type: 'created' | 'edited' | 'iterated' | 'referenced' | 'reverted' | 'comment'
  by?: string
  session_id?: string
  version?: number
  /** For ``reverted`` events: the historical version whose content was
   * copied into the new current version. */
  from_version?: number
  /** Event-type-specific extras. For ``comment`` events:
   * ``action`` (deleted | reviewed | resolved), ``comment_snippet``
   * (≤100-char excerpt of the affected comment), and ``reason``
   * (agent's justification on deletes). */
  metadata?: Record<string, string | number | boolean | null>
}

export interface CommentAnchor {
  quote?: string
  prefix?: string
  suffix?: string
  start_offset?: number
  end_offset?: number
  version_number?: number
}

export interface ArtifactComment {
  id: string
  origin: string
  provider?: string | null
  scope: 'private' | 'shared'
  author: string
  is_agent: boolean
  body: string
  anchor?: CommentAnchor | null
  thread_id: string
  parent_id?: string | null
  status: 'open' | 'review' | 'resolved'
  sync_state: string
  /** True when the anchored text no longer exists in the artifact content
   * (backend rescans anchors on every content write). */
  anchor_orphaned?: boolean
  created_at: string
  updated_at: string
}

// ── WebApp Artifact types (kind="webapp") ────────────────────────────────────

export interface WebAppDeployTarget {
  provider: string;
  account: string;
  region: string;
  public_url: string;
  profile: string;
}

export interface WebAppArchitecture {
  tier: string;
  frontend: string;
  backend: string;
  state: string;
  resources: Array<{ type: string; id: string }>;
}

export interface WebAppLifecycle {
  created_at: string;
  expires_at: string | null;
  persistent: boolean;
  ttl_hours: number;
  status: string;
}

export interface WebAppCost {
  model: string;
  window_hours: number;
  estimates: Array<{ views: number; usd: number }>;
  idle_usd: number;
  note: string;
}

export interface WebAppTeardown {
  method: string;
  handle: string;
  reversible: boolean;
}

export interface WebAppMetadata {
  slug: string;
  origin_session: string;
  /** Local copy of the app tree — powers the gateway's local preview channel. */
  app_dir?: string;
  deploy_target: WebAppDeployTarget;
  architecture: WebAppArchitecture;
  lifecycle: WebAppLifecycle;
  cost: WebAppCost;
  teardown: WebAppTeardown;
}
