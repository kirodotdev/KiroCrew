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

/** The stored record of one active share link. The URL itself is never here. */
export interface Share {
  id: string
  account: string
  section: DriveSection
  key: string
  createdAt: string
  expiresAt: string
  note: string
}

/**
 * Result of `POST /drive/{account}/share`. `url` is shown ONCE, in the copy
 * dialog, and is never persisted client-side beyond that dialog's lifetime.
 */
export interface ShareResult {
  url: string
  share: Share
}

/** Payload of `GET /shares`. */
export interface SharesResponse {
  shares: Share[]
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
export type ArtifactKind = 'widget' | 'markdown' | 'html' | 'json' | 'webapp' | 'image'

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
 * Payload of `GET /backup/{account}`. `runs` holds the last local run per kind;
 * `remote` lists the archive in the bucket (null when it could not be read,
 * with the reason in `remoteError`). `nightly` is the scheduled-snapshot toggle.
 */
export interface BackupStatus {
  nightly: boolean
  runs: Partial<Record<BackupKind, BackupRun>>
  remote: Record<BackupKind, DriveFile[]> | null
  remoteError?: string
}

/** Result of `POST /backup/{account}/run`. */
export interface BackupRunResult {
  ran: true
  kind: BackupKind
  run: BackupRun
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
