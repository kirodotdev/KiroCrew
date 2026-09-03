/**
 * AWS Control API client — a thin same-origin fetch wrapper.
 *
 * Mirrors the personal-shopper client: it prefers the response body's machine
 * readable `code` over the untranslated English `error` prose, so the UI has a
 * stable token to localise (RFC 9457 §3.1.3). The two P0 endpoints are both
 * reads; every mutation in this subsystem goes through the crew or a dashboard
 * confirmation card, not this page.
 */

import type {
  AwsAccountsResponse,
  AvailableProfilesResponse,
  RegisterProfilesResult,
  ReconnectPlan,
  DriveSection,
  DriveStatus,
  DriveBootstrapPreview,
  DriveBootstrapResult,
  DriveListing,
  DriveDownload,
  DriveUploadResult,
  DriveDeleteResult,
  DriveFolderResult,
  DriveFolderDeleteResult,
  ShareResult,
  SharesResponse,
  CostReport,
  LibraryResponse,
  BackupKind,
  BackupStatus,
  BackupRunResult,
  BackupRestoreResult,
  IamPolicyResponse,
} from './types'

import { recordError, findReport, requestPath, type ErrorReport } from '../../utils/errorReport'

const BASE = '/api/apps/aws-control'

/**
 * Error carrying the backend's machine-readable `code` (e.g. `app_disabled`).
 *
 * `message` IS the code — call sites branch on it (`err.message === 'share_active'`)
 * and translate it; the English prose the backend sent beside it is never
 * rendered. That prose still matters to the AGENT, so it travels in `report`:
 * the journal entry `request` recorded for this failure (endpoint, status, code,
 * raw body). An error surface passes it to `ErrorNotice`, which is what lets
 * "ask the agent" carry the real failure rather than the localised sentence the
 * reader saw. Undefined only for an error built outside `request` (tests).
 */
export class AwsControlError extends Error {
  readonly status: number
  readonly report?: ErrorReport
  constructor(code: string, status: number, report?: ErrorReport) {
    super(code)
    this.name = 'AwsControlError'
    this.status = status
    this.report = report
  }
}

/**
 * The journal entry behind a failed call, for an error surface that only holds
 * the thrown value.
 *
 * An `AwsControlError` carries its own. Anything else — the `TypeError` a
 * network failure rejects with — is looked up by message, which is how
 * `request` journals it. Never throws: a surface that cannot recover a report
 * still renders its message, and the hand-off degrades to the sentence alone.
 */
export function errorReportOf(err: unknown): ErrorReport | undefined {
  if (err instanceof AwsControlError) return err.report
  if (err instanceof Error) return findReport(err.message)
  return undefined
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${BASE}${path}`
  let res: Response
  try {
    res = await fetch(url, { credentials: 'same-origin', ...init })
  } catch (e) {
    // A network-level failure never reaches the status branch below, so it is
    // journaled here — by the message the rejection carries, which is what
    // `errorReportOf` looks it back up by. The original error is rethrown
    // unchanged: callers (and tests) key off its own message.
    recordError({
      source: 'api',
      message: e instanceof Error ? e.message : String(e),
      endpoint: requestPath(url),
    })
    throw e
  }
  if (!res.ok) {
    // Read the body ONCE as text: the journal wants it verbatim, and the code is
    // parsed out of the same bytes. An unreadable body (already consumed, a
    // stream that dropped) still yields an `AwsControlError` — the status alone
    // is a usable contract, and this path must never surface a body error.
    let text = ''
    try {
      text = await res.text()
    } catch { /* unreadable body — the status-derived code stands */ }
    let code = `http_${res.status}`
    let prose = ''
    try {
      const body = JSON.parse(text)
      if (body && typeof body.code === 'string') code = body.code
      const msg = body?.error ?? body?.detail ?? body?.message
      if (typeof msg === 'string' && msg.trim()) prose = msg
    } catch { /* non-JSON body */ }
    // `res.url` is the resolved absolute URL on a live response and empty on a
    // synthetic one, so the request's own path is the fallback. Either way the
    // query string is dropped (`requestPath`): an object key or a share note
    // does not belong in a prompt.
    const report = recordError({
      source: 'api',
      message: prose || code,
      status: res.status,
      code,
      endpoint: requestPath(res.url) ?? requestPath(url),
      detail: text,
    })
    throw new AwsControlError(code, res.status, report)
  }
  return (await res.json()) as T
}

/** POST a JSON body and read a JSON reply through the same error contract. */
function postJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
}

const enc = encodeURIComponent

export const awsControlApi = {
  /** Aggregated account list with health + (P0: null) summaries. */
  accounts(refresh = false): Promise<AwsAccountsResponse> {
    return request<AwsAccountsResponse>(`/accounts${refresh ? '?refresh=1' : ''}`)
  },

  /** Local AWS profile NAMES the CLI knows, each flagged whether we registered it. */
  availableProfiles(): Promise<AvailableProfilesResponse> {
    return request<AvailableProfilesResponse>('/profiles/available')
  },

  /** Register the named local profiles into the registry the accounts list reads. */
  registerProfiles(names: string[]): Promise<RegisterProfilesResult> {
    return postJson<RegisterProfilesResult>('/profiles/register', { names })
  },

  /** Reconnect guidance for a degraded/unknown profile, by profile name. */
  reconnectPlan(name: string): Promise<ReconnectPlan> {
    return request<ReconnectPlan>(`/profiles/${enc(name)}/reconnect-plan`)
  },

  /** The exact IAM permissions to paste when a setup fails on AccessDenied. */
  iamPolicy(): Promise<IamPolicyResponse> {
    return request<IamPolicyResponse>('/iam-policy')
  },

  /* ── Drive ── */

  /** Drive bucket status + usage. `refresh` bypasses the 5-minute usage cache. */
  drive(account: string, refresh = false): Promise<DriveStatus> {
    return request<DriveStatus>(`/drive/${enc(account)}${refresh ? '?refresh=1' : ''}`)
  },

  /** Preview the bucket that would be created (empty body → no side effect). */
  driveBootstrapPreview(account: string): Promise<DriveBootstrapPreview> {
    return postJson<DriveBootstrapPreview>(`/drive/${enc(account)}/bootstrap`, {})
  },

  /** Create the bucket after the owner confirms the preview. */
  driveBootstrapConfirm(account: string): Promise<DriveBootstrapResult> {
    return postJson<DriveBootstrapResult>(`/drive/${enc(account)}/bootstrap`, { confirm: true })
  },

  /** One page of a section's folder listing. */
  driveList(
    account: string,
    section: DriveSection,
    path = '',
    token = '',
  ): Promise<DriveListing> {
    const q = new URLSearchParams({ section })
    if (path) q.set('path', path)
    if (token) q.set('token', token)
    return request<DriveListing>(`/drive/${enc(account)}/list?${q.toString()}`)
  },

  /** A short-lived presigned download URL for one file. */
  driveDownload(account: string, section: DriveSection, key: string): Promise<DriveDownload> {
    const q = new URLSearchParams({ section, key })
    return request<DriveDownload>(`/drive/${enc(account)}/download?${q.toString()}`)
  },

  /** Move one stored object inside the files section (server-side copy, then
   *  delete — the delete only happens after the copy succeeded; the backend
   *  refuses to overwrite an existing destination with a 409). */
  driveMove(account: string, section: DriveSection, fromKey: string, toKey: string): Promise<{ moved: true }> {
    return postJson<{ moved: true }>(`/drive/${enc(account)}/move`, { section, fromKey, toKey })
  },

  /** Upload a file's raw bytes to `key` within a section. */
  driveUpload(
    account: string,
    section: DriveSection,
    key: string,
    body: Blob,
  ): Promise<DriveUploadResult> {
    const q = new URLSearchParams({ section, key })
    return request<DriveUploadResult>(`/drive/${enc(account)}/upload?${q.toString()}`, {
      method: 'POST',
      body,
    })
  },

  /** Delete one object from a section. */
  driveDelete(account: string, section: DriveSection, key: string): Promise<DriveDeleteResult> {
    return postJson<DriveDeleteResult>(`/drive/${enc(account)}/delete`, { section, key })
  },

  /**
   * Create a folder.
   *
   * S3 has no directories: an EMPTY folder exists only as a zero-byte object
   * whose key ends in '/', which is what the backend writes. The listing filters
   * that placeholder out of its files, so the folder surfaces through
   * CommonPrefixes like any prefix that happens to hold objects. Idempotent -
   * creating an existing folder is a no-op put.
   */
  driveFolderCreate(account: string, section: DriveSection, path: string): Promise<DriveFolderResult> {
    return postJson<DriveFolderResult>(`/drive/${enc(account)}/folder`, { section, path })
  },

  /**
   * Delete a folder and everything under it.
   *
   * The backend anchors on the section prefix plus a trailing slash (so a
   * sibling `photos-backup/` is not swept up with `photos/`) and pages the batch
   * delete, and it refuses an empty or slash-only path through the same key
   * validator every object key goes through - a folder delete cannot widen into
   * a section- or bucket-wide wipe. `objects` is how many were removed.
   */
  driveFolderDelete(account: string, section: DriveSection, path: string): Promise<DriveFolderDeleteResult> {
    return postJson<DriveFolderDeleteResult>(`/drive/${enc(account)}/folder/delete`, { section, path })
  },

  /** Mint a time-boxed share link. The returned URL is shown once, never stored. */
  driveShare(
    account: string,
    section: DriveSection,
    key: string,
    expiresSecs: number,
    note: string,
  ): Promise<ShareResult> {
    return postJson<ShareResult>(`/drive/${enc(account)}/share`, { section, key, expiresSecs, note })
  },

  /* ── Shares ledger ── */

  /** Active share links for an account. */
  shares(account: string): Promise<SharesResponse> {
    return request<SharesResponse>(`/shares?account=${enc(account)}`)
  },

  /** Forget one share link (removes the record; the link still expires on its own). */
  shareForget(id: string): Promise<{ forgotten: true }> {
    return postJson<{ forgotten: true }>(`/shares/${enc(id)}/forget`, {})
  },

  /* ── Costs ── */

  /** Month-to-date + projected spend. `refresh` bypasses the cache. */
  costs(account: string, refresh = false): Promise<CostReport> {
    return request<CostReport>(`/costs/${enc(account)}${refresh ? '?refresh=1' : ''}`)
  },

  /* ── Library ── */

  /** The account's cloud artifact library, with per-artifact sync state. */
  library(account: string): Promise<LibraryResponse> {
    return request<LibraryResponse>(`/library/${enc(account)}`)
  },

  /** Push one artifact's current version to the bucket. */
  libraryPush(account: string, slug: string): Promise<{ pushed: true }> {
    return postJson<{ pushed: true }>(`/library/${enc(account)}/push`, { slug })
  },

  /** Remove one artifact's cloud copy (objects + ledger entry). */
  libraryRemove(account: string, slug: string): Promise<{ removed: true }> {
    return postJson<{ removed: true }>(`/library/${enc(account)}/remove`, { slug })
  },

  /* ── Backup ── */

  /** Backup status: last local runs, remote archive, nightly toggle. */
  /**
   * Backup status for one account.
   *
   * `remote` defaults to OFF because this endpoint is polled while a run is in
   * flight: its remote half tag-discovers the bucket and lists the archive on
   * every call, so polling it would spend paid AWS round trips to read a fact the
   * server holds in memory. Ask for it only when the stored-archive list is open.
   */
  backup(account: string, opts?: { remote?: boolean }): Promise<BackupStatus> {
    const q = opts?.remote ? '?remote=1' : ''
    return request<BackupStatus>(`/backup/${enc(account)}${q}`)
  },

  /**
   * Start a backup. Returns as soon as the run is claimed, NOT when it finishes:
   * the work is a durable host-owned job, so follow it with the account-scoped
   * `jobs` block on `GET /backup/{account}` rather
   * than by awaiting this call.
   */
  backupRun(account: string, kind: BackupKind): Promise<BackupRunResult> {
    return postJson<BackupRunResult>(`/backup/${enc(account)}/run`, { kind })
  },

  /** Enable/disable the nightly snapshot. */
  backupNightly(account: string, enabled: boolean): Promise<{ nightly: boolean }> {
    return postJson<{ nightly: boolean }>(`/backup/${enc(account)}/nightly`, { enabled })
  },

  /** Restore one archived key into a local staging folder (nothing is hot-swapped). */
  backupRestore(account: string, key: string): Promise<BackupRestoreResult> {
    return postJson<BackupRestoreResult>(`/backup/${enc(account)}/restore`, { key })
  },
}
