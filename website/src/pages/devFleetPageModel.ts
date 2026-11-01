/**
 * The Dev Fleet page's React-free model: the fleet payload types, the sync
 * step-marker protocol and its failure tail, the restart identity handshake and
 * its route-independent watcher, the provision progress model, the prune
 * verdict and status labels, the Switch back dismissal receipt and the small
 * formatters the rows use.
 */
import { i18nT } from '../i18n/t'

const UNDO_DISMISSED_STORAGE_KEY = 'kc-dev-fleet-undo-dismissed'

export function readDismissedUndoKey(): string | null {
  try { return window.sessionStorage.getItem(UNDO_DISMISSED_STORAGE_KEY) }
  catch { return null }
}

export function persistDismissedUndoKey(key: string | null): void {
  try {
    if (key == null) window.sessionStorage.removeItem(UNDO_DISMISSED_STORAGE_KEY)
    else window.sessionStorage.setItem(UNDO_DISMISSED_STORAGE_KEY, key)
  } catch { /* sessionStorage can be unavailable in hardened browsers */ }
}

/* ─── Constants ─── */
export const POLL_MS = 12000
export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

/* ─── Sync step marker protocol ─── */
// The backend tags each of its 5 sync steps ("::step::3::npm ci") so the UI can
// name the step in flight and keep the markers out of the log panel. Progress is
// deliberately NOT quantified: the steps differ in duration by more than an
// order of magnitude and vary with network and cache state, so a percentage
// derived from the step index reads as precision the backend does not have — it
// stalls in one band, then jumps. An indeterminate spinner plus the step name
// and elapsed time is the honest signal.
const STEP_MARKER_RE = /^::step::(\d+)::(.+)$/
// The tail of a FAILED step's stderr, re-emitted by the sync runner under its own
// marker. It exists because output ORDER in the runner's single pipe is not
// evidence: a child block-buffers stdout to a pipe and writes stderr unbuffered,
// so the stdout buffer flushes at EXIT -- after the diagnostic. A refused
// `git merge --ff-only` therefore ENDS with `Updating <old>..<new>`, so a bare
// "last output line" rule names that progress line as the reason Pull+Build
// failed. These markers name the failure from the stream diagnostics arrive on.
const STEPERR_MARKER_RE = /^::steperr::(\d+)::([\s\S]*)$/
// The failure diagnosis arrives on the run as `cause`, derived by the gateway
// from the exit code -- never parsed out of this stream, which also carries
// worktree-controlled build output. `::steperr::` does NOT change that: it is
// the raw log tail, rendered as such, and never sets `lastIsCause`.

export function filterStepMarkers(lines: string[]): string[] {
  // Both markers are protocol. `::steperr::` lines are also DUPLICATES -- the
  // runner already streamed each stderr line into the log -- so dropping them
  // keeps the log panel a faithful transcript rather than one with its tail
  // repeated.
  return lines.filter((l) => !STEP_MARKER_RE.test(l) && !STEPERR_MARKER_RE.test(l))
}

/**
 * The failure text for a finished sync OR provision run, from its output alone.
 *
 * Both runners emit the same `::steperr::` markers for the step that failed, so
 * the two failure notices are named by one rule rather than two.
 *
 * Prefers the failing step's stderr tail (`::steperr::`) over the last output
 * line. The last line is only a good guess when the failing process wrote
 * nothing to stdout: npm prints its diagnosis FIRST and its "a complete log of
 * this run can be found in ..." pointer LAST, and git prints `Updating a..b` to
 * stdout before a refused fast-forward errors on stderr -- so in both cases the
 * final line is the least informative one produced. Falls back to that last-line
 * rule for a run from a gateway that emits no `::steperr::` markers.
 */
export function syncFailureTail(out: string[]): string {
  const stderrTail = out
    .map((l) => STEPERR_MARKER_RE.exec(l)?.[2])
    // Blank texts are dropped, not merely trimmed away later: the runner only
    // ever emits non-blank lines, so a `::steperr::0::` with nothing after it can
    // only be a step printing the marker itself — and an all-blank tail would
    // resolve to `''`, which `ErrorNotice` renders as NOTHING. That would let a
    // build script hide the failure notice, which is a bigger gift than the
    // verbatim-output echo it already had.
    .filter((t): t is string => !!t && !!t.trim())
  if (stderrTail.length) return stderrTail.join('\n')
  // The fallback must exclude BOTH markers, not just `::step::`. A run whose only
  // `::steperr::` lines were blank (the forgery above) leaves them as the last
  // lines of the output, and a fallback that skipped only step markers would
  // then render the raw marker as the failure text.
  return [...out].reverse().find(
    (l) => l?.trim() && !STEP_MARKER_RE.test(l) && !STEPERR_MARKER_RE.test(l),
  ) || ''
}

/* ─── Restart identity handshake ─── */
// POST /restart-gateway and /make-live return the unit's start identity
// captured BEFORE the bounce; GET /apps/dev-fleet/api/health reports the CURRENT
// one. (It must be the /api/ path: the gateway only proxies /apps/dev-fleet/api/*
// to the backend -- the bare /health is the gateway's own internal liveness
// poll and never reaches the browser.) The UI holds "Restarting — reconnecting"
// until it observes a DIFFERENT identity, so a 200 from the OLD process still
// winding down never counts as recovered (the re-click trap this prevents).
const RESTART_TIMEOUT_MS = 60000

// Recovered iff we captured an identity AND the gateway now reports a different
// one. A null captured id (platform can't report identity) or a null/absent
// current id is NOT recovery — the caller degrades or keeps waiting.
export function gatewayRecovered(
  capturedId: string | null | undefined,
  currentId: string | null | undefined,
): boolean {
  if (capturedId == null || currentId == null) return false
  return String(currentId) !== String(capturedId)
}

/* ─── Route-independent restart watcher ─── */
// The restart alive-poll is decoupled from the DevFleetPage React lifecycle so
// navigating away during the ~6-min build+restart phase does not silently kill
// the poll. An AbortController scoped to the active restart (not to the
// component mount) controls cancellation; the only way to abort is an explicit
// user cancel or a new restart superseding the current one.
let _restartAc: AbortController | null = null

// Run IDs with a sync-poll loop currently in flight, tracked at MODULE scope so
// it survives component unmount/remount. The build poll deliberately outlives
// the DevFleet page (a ~6-min build must still auto-restart if the user leaves),
// so a naive remount would start a SECOND poll for the same run — two loops that
// both see `done` and both fire the restart POST. This registry lets a remount
// detect the in-flight poll and skip re-starting one. Cleared when the loop ends.
export const _activeSyncPolls = new Set<string>()


/**
 * Poll the gateway's health endpoint until it comes back with a different
 * start_id, then reload the page. Route-independent: survives React unmount.
 */
export async function awaitGatewayBackGlobal(capturedId: string | null): Promise<'reloaded' | 'timeout' | 'aborted'> {
  _restartAc?.abort()
  const ac = new AbortController()
  _restartAc = ac

  const deadline = Date.now() + RESTART_TIMEOUT_MS
  await sleep(3000)
  while (Date.now() < deadline) {
    if (ac.signal.aborted) return 'aborted'
    try {
      if (capturedId == null) {
        await fetch('/', { signal: AbortSignal.timeout(3000) })
        window.location.reload()
        return 'reloaded'
      }
      const res = await fetch('/apps/dev-fleet/api/health', { credentials: 'same-origin', signal: AbortSignal.timeout(3000) })
      if (res.status === 404) { window.location.reload(); return 'reloaded' }
      if (res.ok) {
        const j = (await res.json().catch(() => null)) as { start_id?: string | null } | null
        if (gatewayRecovered(capturedId, j?.start_id)) { window.location.reload(); return 'reloaded' }
      }
    } catch { /* gateway down mid-bounce */ }
    await sleep(2000)
  }
  return 'timeout'
}

/* ─── Provision progress model ─── */
// The last non-blank output line — the "current activity" shown inline.
// `::steperr::` markers are protocol, not activity: they are skipped so the
// poll that observes them while the run is still `running` does not promote a
// raw marker as the current step.
export function lastLine(lines: string[] | undefined): string {
  if (!lines) return ''
  for (let i = lines.length - 1; i >= 0; i--) {
    const l = lines[i]
    if (l?.trim() && !STEPERR_MARKER_RE.test(l)) return l
  }
  return ''
}

// Coarse phase tag derived from provision.py's markers ("[provision] creating
// venv …" then "[provision] building dist …"). Scans newest→oldest so the tag
// reflects the current step; returns null when nothing recognizable is in view.
export function provPhase(lines: string[] | undefined): string | null {
  if (!lines) return null
  for (let i = lines.length - 1; i >= 0; i--) {
    const l = (lines[i] || '').toLowerCase()
    if (l.includes('building dist') || l.includes('npm run build') || l.includes('vite') || l.includes('tsc ')) return 'dist'
    if (l.includes('creating venv') || l.includes('pip install') || l.includes('venv')) return 'venv'
  }
  return null
}

/**
 * Map a machine prune verdict code to a human-readable reason. Used both for
 * candidate rows and for surfacing WHY a kept row was not pruned. Exported so
 * the mapping can be unit-tested directly.
 */
export function pruneVerdictLabel(code?: string): string {
  switch (code) {
    case 'merged': return i18nT('pages.devFleetPage.pr_merged')
    case 'closed': return i18nT('pages.devFleetPage.pr_closed_not_on_main')
    case 'closed_dirty': return i18nT('pages.devFleetPage.pr_closed_uncommitted_changes')
    case 'closed_new_commits': return i18nT('pages.devFleetPage.pr_closed_but_branch_has_newer_commits')
    case 'closed_unverified': return i18nT('pages.devFleetPage.pr_closed_but_verification_unavailable_retry')
    case 'empty': return i18nT('pages.devFleetPage.no_commits_stale')
    case 'merged_dirty': return i18nT('pages.devFleetPage.pr_merged_uncommitted_changes')
    case 'fresh': return i18nT('pages.devFleetPage.created_recently')
    case 'active': return i18nT('pages.devFleetPage.pr_open_or_unmerged_commits')
    case 'merged_new_commits': return i18nT('pages.devFleetPage.pr_merged_but_new_commits_pushed_after_merge')
    case 'merged_unverified': return i18nT('pages.devFleetPage.pr_merged_but_verification_unavailable_retry')
    case 'dirty_check_failed': return i18nT('pages.devFleetPage.git_status_failed')
    default: return code || ''
  }
}

// Per-item prune status -> visual kind, driving the checklist's icon and badge.
export const PRUNE_STATUS_META: Record<string, { kind: 'idle' | 'spin' | 'done' | 'failed' }> = {
  pending: { kind: 'idle' },
  verifying: { kind: 'spin' },
  stopping_pod: { kind: 'spin' },
  removing: { kind: 'spin' },
  done: { kind: 'done' },
  failed: { kind: 'failed' },
}

/**
 * Catalog KEY for each prune status's chip label — kept in its own flat table,
 * beside PRUNE_STATUS_META (add a status to both).
 *
 * Keys, not strings: this is module scope, evaluated once at import, so an
 * `i18nT()` call here would freeze the boot language. `pruneStatusLabel()` does
 * the lookup during render. Flat `Record` of full literal keys indexed inline at
 * the `i18nT()` call, because that is the form `scripts/check-i18n-keys.mjs` can
 * resolve statically. `removing` / `failed` reuse the keys this page already
 * ships for those two words rather than adding duplicates.
 */
const PRUNE_STATUS_LABEL_KEY: Record<string, string> = {
  pending: 'pages.devFleetPage.pending',
  verifying: 'pages.devFleetPage.verifying',
  stopping_pod: 'pages.devFleetPage.stopping_pod',
  removing: 'pages.devFleetPage.removing',
  done: 'pages.devFleetPage.removed',
  failed: 'pages.devFleetPage.failed',
}

/** Localised chip label for a prune status, falling back to the `pending` copy for
 *  the same reason the caller falls back to its meta — the status arrives from the
 *  /api/run poll, so an unrecognised value must still render something.
 *
 *  `hasOwnProperty`, not `in`: a status of `toString` would otherwise resolve to an
 *  inherited Object.prototype member and hand a function to i18next. */
export function pruneStatusLabel(status: string): string {
  return Object.prototype.hasOwnProperty.call(PRUNE_STATUS_LABEL_KEY, status)
    ? i18nT(PRUNE_STATUS_LABEL_KEY[status])
    : i18nT(PRUNE_STATUS_LABEL_KEY.pending)
}

export function fmtElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0')
}

// Colour for the memory readout as the pod approaches its cgroup MemoryMax.
// Crossing MemoryMax is an OOM kill, so the readout shifts warn -> danger as
// the ratio climbs. No ceiling (mem_max absent) -> neutral, since there is
// nothing to be close to.
export function memColor(current: number | null | undefined, max: number | null | undefined): string {
  if (current == null || max == null || max <= 0) return 'var(--muted)'
  const ratio = current / max
  if (ratio >= 0.9) return 'var(--danger)'
  if (ratio >= 0.75) return 'var(--warn)'
  return 'var(--muted)'
}

export interface PodResources {
  mem_current?: number | null
  mem_max?: number | null
  cpu_pct?: number | null
  tasks?: number | null
  home_bytes?: number | null
}

interface FleetTotals {
  pod_home_bytes?: number | null
  orphan_pods?: number | null
}

/* ─── Types ─── */
export interface IssueRef { number: number; url?: string | null }
export interface TicketRef { id: string; url?: string | null }
interface PrInfo { number?: number; state?: string; url?: string; isDraft?: boolean; title?: string }
export interface Worktree {
  name: string; branch?: string; is_main?: boolean; running?: boolean
  has_dist?: boolean; dirty?: boolean; port?: number; health?: number; behind?: number
  last_updated_at?: number
  pr?: PrInfo | null; shipped?: boolean
  issues?: IssueRef[]; tickets?: TicketRef[]; summary?: string | null
  own_commits?: number; real_dirty?: boolean; is_live?: boolean; is_staged?: boolean; legacy?: boolean
  // Breakdown of what makes the tree dirty, from the detail payload. A tree
  // whose only dirt is untracked files (dirty_tracked === false) is removable
  // by discarding them; one with modified tracked files is not.
  dirty_tracked?: boolean | null; dirty_untracked?: number; dirty_untracked_paths?: string[]
  path?: string
  provision_run_id?: string | null
  // Per-pod system resources (running pods on Linux only); absent otherwise.
  pod_resources?: PodResources | null
}
export interface UndoTarget { name: string; path: string }
// One published release channel and where its detached worktree sits.
// `worktree` is non-null ONLY for a tree the backend is willing to drive as a
// channel pin (it exists AND is detached), so this field — not the row's name —
// is what decides whether a row gets channel treatment. `name_taken_by_branch`
// is the third state: the reserved name is occupied by somebody's branch
// checkout, so Create cannot run and the UI has to say why.
export interface ReleaseChannel {
  // The channel's name, for the sentences that name it on screen. Display data:
  // no request carries it back, because there is exactly one channel and the
  // endpoint takes no argument.
  lane: string
  // The basename this channel's worktree has or would have, supplied by the
  // backend so the naming rule lives on ONE side of the boundary. Re-deriving
  // it here would let a change to WORKTREE_NAME desync this label from the real
  // path.
  name: string
  worktree?: string | null
  ref?: string | null
  // The release THIS ROW is on: on an adopted row the one its tree actually
  // holds, on a placeholder the one Create would check out. `tip_version` is
  // always the channel's resolved tip, so a behind row names both without
  // either being ambiguous — and a `null` version on an adopted row is a real
  // state (the tree is detached at no release tag), not a missing value to fill
  // in from the tip.
  version?: string | null
  tip_version?: string | null
  // A benign, documented state kept apart from `error`: this checkout has
  // fetched no release tag yet. It renders as ordinary information and leaves
  // Create enabled, because Create fetches first and a fetch is what resolves
  // it. `error` stays reserved for a genuine git failure, which reaches the
  // shared ErrorNotice.
  unpublished?: boolean
  error?: string | null
  // At-tip is a boolean, not a distance: the payload carries no channel-tip
  // commit count, because every rendering of one (`↓3`, `↓3 tip`, a second
  // denominator in the Behind column) left two cells in one column meaning two
  // things. The row states currency by naming both versions instead.
  at_tip?: boolean | null
  // On an adopted row a `null` version means one of TWO things, and the badge must
  // not say the stronger one when only the weaker is known. `false`: the tree is
  // detached at no published release — the positive answer, "not on a release".
  // `true`: the probe that reads the tree's release failed (a timed-out or
  // unreadable `--points-at` listing); the row carries the error and the badge
  // says the version is unknown, asserting nothing about a tree nobody read.
  version_unknown?: boolean
  name_taken_by_branch?: boolean
}
export interface FleetData { worktrees: Worktree[]; error?: string; needs_setup?: boolean; main_repo?: string; main_repo_inferred?: boolean; base_branch?: string; sync_run_id?: string; build_pending?: boolean; gateway_service_active?: boolean; gateway_service_reason?: string | null; pods_available?: boolean; pods_unavailable_reason?: string | null; serving_install_reason?: string | null; staged_target?: string | null; staged_cancel_available?: boolean; undo_target?: UndoTarget | null; live_state_known?: boolean; live_verified?: boolean; manual_restart?: string; fleet_totals?: FleetTotals; release_channel?: ReleaseChannel | null }
// `lastIsCause` distinguishes the two things `last` can hold. A gateway-composed
// diagnosis is decision-critical prose ending in the action to take, so it must
// not render in the muted 11.5px monospace the raw log tail uses.
export interface SyncRun { rid: string; status: 'running' | 'done' | 'error'; lines: string[]; startedAt: number; exit?: number | null; last?: string; lastIsCause?: boolean; stepLabel?: string }
// Provision run state: the FULL output is kept (not just the last
// line) so the expandable log panel can show everything, and a failed run
// persists (failed=true) until the user dismisses it rather than vanishing.
export interface ProvRun { rid?: string; status: 'starting' | 'running' | 'done' | 'failed'; lines: string[]; startedAt: number; exit?: number | null; failed?: boolean; done?: boolean }
export interface RebaseResult { kind: 'ok' | 'conflict' | 'error'; text: string }

export type PruneDialog = { candidates: { name: string; code?: string; unmerged_commits?: boolean }[]; kept: { name: string; code?: string; dirty?: boolean; dirty_tracked?: boolean | null; dirty_untracked?: number; dirty_untracked_paths?: string[] }[]; scanned: number }
export type PruneProgress = { names: string[]; items: Record<string, { status: string; error?: string | null }>; done: number; total: number; running: boolean }

/* ─── Release channel row classification ─── */
// Which row (if any) a `release_channel` payload describes is decided HERE, in
// one React-free place, so the page, the row cells and the tests all read the
// same rules. Every helper takes the payload explicitly and returns the channel
// it applies to (or null/undefined), so a caller cannot accidentally treat a
// row as a channel pin because its NAME looks like one.

/** The adopted channel worktree's name, or null when no tree is adopted. */
export function channelWorktreeName(rc: ReleaseChannel | null | undefined): string | null {
  return rc?.worktree || null
}

/**
 * The channel this row IS, or undefined.
 *
 * Adopted only. A row is a channel pin because the BACKEND says its tree is
 * detached at a resolved ref, never because its name looks like one — so a
 * user's own `release-channel-stable` branch checkout keeps ordinary controls.
 */
export function channelForRow(
  rc: ReleaseChannel | null | undefined,
  w: Worktree,
): ReleaseChannel | undefined {
  const adopted = channelWorktreeName(rc)
  return !w.is_main && adopted && w.name === adopted ? (rc as ReleaseChannel) : undefined
}

/**
 * The channel whose reserved basename THIS row is occupying with a branch
 * checkout, or undefined.
 *
 * The explanation lands on the row that actually exists: one directory is one
 * row, and rendering a second placeholder for it printed the same name twice.
 */
export function channelNameTakenFor(
  rc: ReleaseChannel | null | undefined,
  name: string,
): ReleaseChannel | undefined {
  return rc && !rc.worktree && rc.name_taken_by_branch && rc.name === name ? rc : undefined
}

/**
 * The third state the backend can emit: the reserved directory EXISTS and is a
 * registered worktree (so it renders as an ordinary selectable row), but its git
 * state could not be read — `worktree` is null, `name_taken_by_branch` is false,
 * and only `error` is set. Neither `channelForRow` nor `channelNameTakenFor`
 * fires for it, and because the row IS in the fleet the placeholder path is
 * suppressed too, so without this the error string has no surface at all.
 */
export function channelUnreadableFor(
  rc: ReleaseChannel | null | undefined,
  name: string,
): ReleaseChannel | undefined {
  return rc && !rc.worktree && !rc.name_taken_by_branch && rc.error && rc.name === name ? rc : undefined
}

/**
 * ONE derivation for every channel resolver error, so a single ErrorNotice is
 * the sole surface for both cases: an ADOPTED row whose resolve() failed, and
 * the reserved directory whose HEAD could not be read. A tooltip is neither
 * keyboard-reachable nor an agent hand-off, so no error may live in a Badge
 * title.
 */
export function channelErrorFor(
  rc: ReleaseChannel | null | undefined,
  w: Worktree,
): ReleaseChannel | undefined {
  const adopted = channelForRow(rc, w)
  if (adopted?.error) return adopted
  return channelUnreadableFor(rc, w.name)
}

/**
 * The channel that needs a Create placeholder row, or null.
 *
 * Suppressed whenever a real directory already carries the reserved basename,
 * whatever the backend made of it. `worktree` and `name_taken_by_branch` are
 * both CLASSIFICATIONS, and a third state exists: an unreadable HEAD resolves to
 * neither — so a guard testing only those two rendered the placeholder beside
 * the very directory it was describing, and one directory became two rows.
 * Testing the fleet for the NAME is the check that holds for every
 * classification, including ones added later, because the name is the thing that
 * can only belong to one row.
 *
 * Against the UNFILTERED fleet: the search box and the legacy toggle decide what
 * is on screen, and a query that hides the channel row must not conjure a Create
 * placeholder for a directory that exists.
 */
export function channelPlaceholderFor(
  rc: ReleaseChannel | null | undefined,
  wts: Worktree[],
): ReleaseChannel | null {
  if (!rc || rc.worktree || rc.name_taken_by_branch) return null
  const present = wts.some((w) => !w.is_main && w.name === rc.name)
  return present ? null : rc
}
