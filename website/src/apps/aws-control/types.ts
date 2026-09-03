/**
 * Wire types for the AWS Control Accounts page (P0).
 *
 * These mirror the shapes `backend.routes` returns for
 * `GET /api/apps/aws-control/accounts` and
 * `GET /api/apps/aws-control/profiles/{name}/reconnect-plan`. Everything here is
 * read-only in P0: the page never mutates, so there are no request bodies.
 */

/** A single AWS account's health as reported by the read-only identity probe. */
export type AccountHealth = 'ok' | 'degraded' | 'unknown'

/** How a profile resolves its credentials — decides which Reconnect guidance applies. */
export type ProfileKind = 'sso' | 'credential-process' | 'other'

/**
 * One credential profile ("key") belonging to an account. Names and regions
 * only — credential material is never read or transmitted (spec G2).
 */
export interface AwsProfile {
  name: string
  region: string
  kind: ProfileKind
  identityOk: boolean
  account: string
  arn: string
  detail: string
  default: boolean
}

/**
 * Per-account rolled-up figures. Every field is null in P0 — nothing is measured
 * yet — so the UI must render an em dash, never a zero. The shape is fixed now so
 * later phases can fill it without a wire change.
 */
export interface AwsAccountSummary {
  storage: number | null
  sites: number | null
  tasks: number | null
  costMonthToDate: number | null
}

/**
 * One account row. `account` is the 12-digit id, or "" for the unresolved
 * pseudo-row (a profile whose identity could not be resolved to an account);
 * the backend always returns that row last. `name` is the default profile's
 * name (may be "" for the unresolved pseudo-row), and is what the row leads with.
 */
export interface AwsAccount {
  account: string
  name: string
  health: AccountHealth
  profiles: AwsProfile[]
  summary: AwsAccountSummary
}

/** Cross-account totals for the summary strip. */
export interface AwsTotals {
  accounts: number
  profiles: number
  profilesHealthy: number
}

/** Payload of `GET /accounts`. */
export interface AwsAccountsResponse {
  accounts: AwsAccount[]
  totals: AwsTotals
  generatedAt: string
}

/**
 * One local profile the AWS CLI reports, flagged with whether the portal has
 * already registered it. Names only — no credential material (spec G2).
 */
export interface AvailableProfile {
  name: string
  registered: boolean
}

/**
 * Payload of `GET /profiles/available`. `supported` is false on a platform that
 * cannot enumerate local profiles (Windows), where an empty `profiles` must be
 * read as "can't tell", not "none" — the UI says so instead of implying the
 * operator has no accounts. `registeredCount`/`max` bound the registry so the
 * picker can show a count hint and stop offering more once the cap is reached.
 */
export interface AvailableProfilesResponse {
  profiles: AvailableProfile[]
  registeredCount: number
  max: number
  supported: boolean
}

/**
 * Payload of `POST /profiles/register`. A batch registers the prefix that fits
 * under the cap, so `added + skipped` counts the whole request, not just the
 * winners. Error codes: `invalid_names` (400), `unknown_profile` (400).
 */
export interface RegisterProfilesResult {
  added: number
  skipped: number
}

/**
 * Payload of `GET /profiles/{name}/reconnect-plan`.
 *
 * `command` is a literal shell command and is never translated. `method` is
 * `terminal` in P0 (SSO device-flow automation lands in a later phase).
 */
export interface ReconnectPlan {
  method: string
  kind: ProfileKind
  command: string
}

/* ── Account Console ──────────────────────────────────────────────────────
 * Wire types for the per-account console (the view that opens when an account
 * row is clicked). Every shape mirrors what `backend.routes` returns for the
 * drive / costs / library / backup / shares endpoints. All are owner-only and
 * same-origin; a mutation always ends by invalidating its react-query key.
 */

/** A section of the drive bucket. The three are laid over ONE S3 bucket. */
export type DriveSection = 'drive' | 'library' | 'backup'

/** Per-section object + byte tallies inside the drive bucket's usage report. */
export interface DriveSectionUsage {
  objects: number
  bytes: number
}

/** Rolled-up bucket usage. `sections` splits the total by prefix. */
export interface DriveUsage {
  bytes: number
  objects: number
  sections: Record<DriveSection, DriveSectionUsage>
}

/**
 * Payload of `GET /drive/{account}`. Before the bucket is created the whole
 * body is `{exists:false}`; once created it also carries the bucket name, its
 * region and a (5-minute cached) usage report.
 */
export type DriveStatus =
  | { exists: false }
  | {
      exists: true
      bucket: string
      region: string
      usage: DriveUsage
    }

/** Preview payload of `POST /drive/{account}/bootstrap` with an empty body. */
export interface DriveBootstrapPreview {
  preview: true
  account: string
  region: string
  resource: string
}

/** Result of `POST /drive/{account}/bootstrap` with `{confirm:true}`. */
export interface DriveBootstrapResult {
  created: true
  bucket: string
}

/** One stored object in a section listing. `key` is section-relative. */
export interface DriveFile {
  key: string
  size: number
  modified: string
}

/** Payload of `GET /drive/{account}/list`. `nextToken` paginates when present. */
export interface DriveListing {
  files: DriveFile[]
  folders: string[]
  nextToken?: string
}

/** Payload of `GET /drive/{account}/download` — a short-lived presigned URL. */
export interface DriveDownload {
  url: string
  expiresSecs: number
}

/** Result of `POST /drive/{account}/upload`. */
export interface DriveUploadResult {
  uploaded: true
  key: string
  bytes: number
}

/** Result of `POST /drive/{account}/delete`. */
export interface DriveDeleteResult {
  deleted: true
}

/** Result of `POST /drive/{account}/folder`. `path` echoes the folder created. */
export interface DriveFolderResult {
  created: true
  path: string
}

/**
 * Result of `POST /drive/{account}/folder/delete`.
 *
 * `objects` is the number actually removed, which the page reports after the
 * delete: one click can remove many objects, and this count is the only honest
 * statement of what happened - a figure shown BEFORE consent would cost a
 * second full recursive listing of the prefix.
 */
export interface DriveFolderDeleteResult {
  deleted: true
  path: string
  objects: number
}

/** The stored record of one active share link. The URL itself is never here. */
export interface Share {
  id: string
  account: string
  section: DriveSection
  key: string
  createdAt: string
  expiresAt: string
  note: string
  /**
   * The object this share points at was not in the drive when the row was
   * rendered. Present ONLY when established: absent means either "the object
   * is there" or "the drive was not read", which `SharesResponse.checked`
   * tells apart.
   *
   * The row is marked rather than removed because the ledger records that an
   * unexpired URL was minted, and deleting the object does not un-mint it —
   * re-creating the key makes the same URL resolve again.
   */
  objectMissing?: boolean
}

/**
 * Result of `POST /drive/{account}/share`. `url` is shown ONCE, in the copy
 * dialog, and is never persisted client-side beyond that dialog's lifetime.
 */
export interface ShareResult {
  url: string
  share: Share
}

/**
 * Payload of `GET /shares`. `checked` says whether these rows were actually
 * compared against the account's drive — without it an absent `objectMissing`
 * would read as "the object is there" on a render where the drive was never
 * read. WHY it was not checked is logged server-side, not sent: the reason is a
 * backend-authored English sentence and this surface is localized, so the note
 * the console shows is a translated string gated on this flag.
 */
export interface SharesResponse {
  shares: Share[]
  checked?: boolean
}

/** One line of the cost breakdown by AWS service. */
export interface CostByService {
  service: string
  amount: number
}

/**
 * Payload of `GET /costs/{account}`. `consentMissing` marks the case where the
 * Cost Explorer consent gate has not been granted, so no figures were fetched;
 * `fetchError` marks a real call failure. `fresh:false` means the numbers came
 * from cache and should carry an "as of" hint.
 */
export interface CostReport {
  fresh: boolean
  monthToDate: number
  projected: number
  currency: string
  byService: CostByService[]
  fetchedAt: string
  consentMissing?: boolean
  fetchError?: string
}

/** Artifact kinds the library can hold. `image` cannot be pushed. */
/**
 * Artifact kinds, mirroring ALLOWED_KINDS in the backend artifact store.
 *
 * ALL EIGHT, deliberately. `list_pushable` returns `artifact.kind`
 * verbatim without filtering, and `_KIND_EXT` makes svg and text
 * PUSHABLE -- so omitting them did not make them unreachable, it only
 * stopped the compiler from noticing that their kind badge rendered
 * blank. Keep this in step with the backend set.
 */
export type ArtifactKind =
  | 'widget' | 'markdown' | 'html' | 'svg' | 'json' | 'text' | 'webapp' | 'image'

/**
 * One artifact in the account's cloud library. `pushedVersion` is the version
 * already synced to the bucket (null when never synced); the tile is
 * up-to-date when `pushedVersion === version`.
 */
export interface LibraryArtifact {
  slug: string
  name: string
  kind: ArtifactKind
  version: number
  updatedAt: string
  pushedVersion: number | null
  pushedAt: string | null
}

/** Payload of `GET /library/{account}`. */
export interface LibraryResponse {
  artifacts: LibraryArtifact[]
}

/** One backup run's most recent artifact. */
export interface BackupRun {
  key: string
  bytes: number
  at: string
}

/** The two backup kinds: a workspace/memory snapshot and a sessions archive. */
export type BackupKind = 'snapshot' | 'sessions'

/**
 * One kind's durable-job state for ONE account, as `GET /backup/{account}` serves
 * it. Mirrors `routes._job_view`.
 *
 * `active` is what a fresh mount adopts: the run is the host's fact, so a reload
 * or a navigation away and back still finds it. `lastFailed` exists because the
 * app's own `runs` ledger only gains an entry when an upload SUCCEEDS -- without
 * it a failed run would leave the row silent, indistinguishable from one that
 * never started.
 *
 * Account-scoped deliberately. The shared `_jobs/active` surface is app-scoped by
 * construction and withholds the account, so it cannot answer "is a backup
 * running for THIS account" -- which is the only question this page asks.
 */
export interface BackupJobRun {
  run_id: string
  kind: BackupKind
  status: string
  created_at: string
  updated_at: string
  finished_at: string
  error: string
}

export interface BackupJobState {
  active: BackupJobRun | null
  lastFailed: BackupJobRun | null
}

/**
 * Payload of `GET /backup/{account}`. `runs` holds the last local run per kind;
 * `remote` lists the archive in the bucket (null when it could not be read,
 * with the reason in `remoteError`). `nightly` is the scheduled-snapshot toggle.
 * `jobs` carries the in-flight and last-failed run per kind for this account.
 */
export interface BackupStatus {
  nightly: boolean
  runs: Partial<Record<BackupKind, BackupRun>>
  jobs?: Partial<Record<BackupKind, BackupJobState>>
  remote: Record<BackupKind, DriveFile[]> | null
  remoteError?: string
}

/**
 * Result of `POST /backup/{account}/run`.
 *
 * A HANDLE to work in flight, not an outcome: the backup is a durable host-owned
 * job, so the response arrives while it is still running and `runId` is how the
 * UI re-finds it after a reload. What the backup PRODUCED lands in the app's own
 * ledger and is read back through `GET /backup/{account}` as `runs`.
 */
export interface BackupRunResult {
  started: true
  kind: BackupKind
  runId: string
}

/**
 * Result of `POST /backup/{account}/restore`. Nothing is hot-swapped: the
 * archive is downloaded to a local staging folder and `path` is where it landed.
 */
export interface BackupRestoreResult {
  downloaded: true
  path: string
  bytes: number
}

/** Payload of `GET /iam-policy` — the exact permissions to paste, as JSON text. */
export interface IamPolicyResponse {
  policy: string
}
