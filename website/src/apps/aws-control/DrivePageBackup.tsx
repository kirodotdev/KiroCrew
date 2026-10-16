/**
 * AWS Control drive - the Backup pane: one row per backup kind with its run
 * state and manual run, the two nightly consent switches, this install's label,
 * and the stored-archive list with restore. `DrivePage.tsx` re-exports
 * `BackupSection`.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
// By path, not from the app-sdk barrel: that barrel is published to third-party
// apps through the vendor stub, and a host-namespaced cache client is not part
// of that contract.
import { useAppQuery, useAppQueryKey } from '../../app-sdk/appQuery'
import {
  ChevronDown, RefreshCw, Archive, Download, AlertTriangle, Pencil, Database,
} from 'lucide-react'
import {
  Btn, Badge, Toggle, Input, ContentSkeleton, Card, PanelSectionHeader,
} from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { fmtBytes, fmtRelative } from '../../i18n/format'
import { awsControlApi } from './api'
import type { BackupKind, BackupRun, BackupJobState, BackupArchive, RemoteBackup } from './types'
import { CopyBtn, PaneHeader, AwsErrorNotice } from './shared'

/* Literal-key maps from enum → full catalog key, so no i18nT() call assembles a
 * key by interpolation (dynamicKeys gate): extractors and unused-key tooling
 * can then see every key, and a missing one fails the parity gate rather than
 * rendering raw. Mirrors UPDATE_ERROR_KEYS in pages/settings/AboutPanel.tsx. */

/* Refusals that name a cause the owner can act on, mapped rather than collapsed
 * into one line. The route answers these with codes precisely so the UI can
 * localise them; discarding the code and rendering a single generic string makes
 * the owner guess which of several different repairs to attempt.
 *
 * `invalid_account` is deliberately absent, and the asymmetry with `drive_missing`
 * is the point rather than an exception to it: the account comes from the page, so
 * the start button structurally cannot produce a malformed one -- an IMPOSSIBLE
 * state. `drive_missing` past the pane gate is a RACE (the drive deleted between
 * the pane rendering and the click), and rare is not impossible. It falls to the
 * generic line with any code we do not recognise. */
const START_ERROR_KEYS: Record<string, string> = {
  aws_consent_required: 'apps.awsControl.console.backup_start_consent',
  drive_missing: 'apps.awsControl.console.backup_start_no_drive',
  jobs_unavailable: 'apps.awsControl.console.backup_start_unavailable',
}

/**
 * Why the nightly transcript archive is not running, in the reader's language.
 *
 * The backend sends a stable code, not a sentence: it cannot know the reader's
 * locale, so a sentence chosen there is English on every install. An unrecognised
 * code falls through to the general line rather than rendering nothing, because a
 * newer gateway can name a condition this build has never heard of -- and "on
 * hold" is true of all of them, which is far better than letting the page imply
 * the backup is fine.
 *
 * A switch over static map access rather than an indexed lookup, so every key
 * here stays findable by the i18n gate instead of counting as a dynamic site.
 */
const BACKUP_BLOCKED_KEYS = {
  host: 'apps.awsControl.console.backup_nightly_sessions_blocked_host',
  redaction: 'apps.awsControl.console.backup_nightly_sessions_blocked_redaction',
  otherAccount: 'apps.awsControl.console.backup_nightly_sessions_blocked_other_account',
  other: 'apps.awsControl.console.backup_nightly_sessions_blocked_other',
} as const

/* The two "you can still start it yourself" lines, literal for the same reason as
   the map above: named property access keeps each key findable by the i18n gate. */
const BACKUP_NEXT_KEYS = {
  generic: 'apps.awsControl.console.backup_nightly_sessions_blocked_next',
  redaction: 'apps.awsControl.console.backup_nightly_sessions_blocked_next_redaction',
} as const

function nightlySessionsBlockedText(code: string): string {
  switch (code) {
    case 'host_unsupported': return i18nT(BACKUP_BLOCKED_KEYS.host)
    case 'redaction_on': return i18nT(BACKUP_BLOCKED_KEYS.redaction)
    case 'other_account': return i18nT(BACKUP_BLOCKED_KEYS.otherAccount)
    default: return i18nT(BACKUP_BLOCKED_KEYS.other)
  }
}

/* Which "you can still start it yourself" line to offer under the notice, or
   null for none. The manual run is per-account and per-kind, so it is a real next
   step for a grant withheld by a setting or recorded on an account the schedule
   does not visit -- and NOT one on a host that cannot produce the archive at all,
   where that same run refuses with the same capability answer. Naming a click
   that fails is worse than naming none.

   `redaction_on` gets its OWN line because there the manual run's outcome is the
   very thing the notice above it withheld: the nightly stands down precisely
   because the sessions archive has no redaction seam (it has two bundle roots, so
   `prepare_redacted_copy` refuses it), and the button uploads those same bytes
   unredacted. The backend calls that owner "present and choosing knowingly", which
   is only true if the page says what the choice is -- and the operator reading this
   notice is by definition one who asked for redaction. */
function nightlySessionsBlockedNextText(code: string): string | null {
  if (code === 'host_unsupported') return null
  if (code === 'redaction_on') return i18nT(BACKUP_NEXT_KEYS.redaction)
  return i18nT(BACKUP_NEXT_KEYS.generic)
}

const BACKUP_KIND_LABEL_KEY: Record<BackupKind, string> = {
  snapshot: 'apps.awsControl.console.backup_kind_snapshot',
  sessions: 'apps.awsControl.console.backup_kind_sessions',
}

const BACKUP_KINDS: BackupKind[] = ['snapshot', 'sessions']

/** The glyph each backup kind is recognised by. A snapshot is an archive of the
 *  workspace; the sessions archive is a database dump, so they are not the same
 *  picture. */
const BACKUP_KIND_ICON: Record<BackupKind, React.ReactNode> = {
  snapshot: <Archive className="h-4 w-4" aria-hidden="true" />,
  sessions: <Database className="h-4 w-4" aria-hidden="true" />,
}

/**
 * One backup kind's row.
 *
 * Its own component, rather than a `.map` body inside `BackupSection`, so each
 * row's mutation state stays its own -- a click on one kind must not disable the
 * other.
 *
 * `busy` comes from the SERVER, not from `runMut.isPending`. That is the whole
 * change: the previous indicator lived in this component, so unmounting it
 * destroyed the only record that a backup was in flight, and coming back showed
 * an idle row while the upload continued. Under the rail that unmount is no
 * longer hypothetical -- switching panes genuinely unmounts this subtree.
 */
function BackupRow({
  account,
  kind,
  run,
  remembered,
  job,
  unsupported,
  onStarted,
  onShowArchives,
}: {
  account: string
  kind: BackupKind
  run: BackupRun | undefined
  /* How many archives of this kind the install still holds a record of, or
     undefined from a backend that does not report it. A record count, not the
     drive's contents in either direction, so the row sends the reader to the
     list for what is actually there. */
  remembered: number | undefined
  job: BackupJobState | undefined
  /* This host cannot produce this kind's payload at all. The manual run answers
     the same capability question and refuses with 501, so a pressable button here
     offers work that cannot happen -- and the notice under the switch already
     says why, which is the sentence a disabled button sends the reader to. */
  unsupported: boolean
  onStarted: () => void
  /* Opens the stored-archive disclosure below, which is the only surface that
     lists what the drive actually holds. The count line is a shortcut to it,
     placed where the reader is already looking at a number. */
  onShowArchives: () => void
}) {
  const runMut = useMutation({
    mutationFn: () => awsControlApi.backupRun(account, kind),
    // The claim is durable the moment the POST returns, but the next poll may be
    // seconds away. Re-read now so the row turns over immediately instead of
    // looking like the click did nothing.
    onSuccess: onStarted,
  })
  // The server owns the answer, scoped to THIS account. `isPending` still matters
  // for the window between the click and the claim landing: the server does not
  // know about the run yet, and a row that ignored it would accept a second click
  // that starts nothing (the SDK would dedupe it).
  const busy = job?.active != null || runMut.isPending
  // A run that ended `failed` must not render identically to one that succeeded.
  // The app's ledger records only successes, so without this the row would simply
  // stop spinning -- which is itself a false statement about what happened.
  const failed = !busy ? (job?.lastFailed ?? null) : null
  // Which refusals a retry can actually clear, enumerated from the route rather
  // than special-cased one at a time. `aws_call_failed` is a 502 from a live AWS
  // call and is the only genuinely transient one; a transport-level `http_5xx`
  // is the same shape. Everything else the start path answers needs the owner to
  // do something ELSE, so it gets the cause named (START_ERROR_KEYS) or, for a
  // code we do not recognise, a line that promises nothing. Defaulting to "try
  // again" and excepting one code was the wrong way round.
  const code = (runMut.error as Error | null)?.message ?? ''
  const retryable = code === 'aws_call_failed' || /^http_5\d\d$/.test(code)
  const startError = runMut.isError
    ? i18nT(
        START_ERROR_KEYS[code] ??
          (retryable
            ? 'apps.awsControl.console.backup_start_retry'
            : 'apps.awsControl.console.backup_start_failed'),
      )
    : ''

  return (
    <div className="flex flex-wrap items-center gap-3 px-3 py-2.5" data-testid={`backup-row-${kind}`}>
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-bg-elevated text-muted">
        {BACKUP_KIND_ICON[kind]}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium text-text">{i18nT(BACKUP_KIND_LABEL_KEY[kind])}</div>
        <div className="text-[12px] text-muted" data-testid={`backup-last-${kind}`}>
          {run
            ? i18nT('apps.awsControl.console.backup_last_run', { when: fmtRelative(run.at), size: fmtBytes(run.bytes) })
            : // "Not backed up yet" alone contradicts a nonzero recorded count on
              // the same row: the ledger keeps one run record per kind and holds
              // only this install's, so a record aged out of it -- or made by a
              // reinstall under the same name -- leaves archives on the drive with
              // no run line. When the count line below will say archives exist,
              // this one says which kind of "none" it means: no run recorded here,
              // not no archive kept. With nothing recorded either, the plain line
              // still reads correctly.
              remembered != null && remembered > 0
              ? i18nT('apps.awsControl.console.backup_never_recorded')
              : i18nT('apps.awsControl.console.backup_never')}
        </div>
        {/* The line above reports ONE run, because the ledger keeps one record per
            kind: a second nightly overwrites the first while both archives stay in
            the drive. Alone it therefore reads as a drive holding one archive. This
            count is what the install has a record of -- not the drive's contents --
            and it opens the list that can say more.

            Rendered only when the count EXCEEDS what the line above implies: a run
            line already accounts for one archive, so `> 1` there and `> 0` on a row
            that shows no run at all. A row whose two lines would agree says nothing
            the first line did not, and permanent furniture on every backed-up row
            is not a discrepancy signal. */}
        {remembered != null && remembered > (run ? 1 : 0) && (
          <button
            onClick={onShowArchives}
            className="text-[12px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 text-left"
            data-testid={`backup-remembered-${kind}`}
          >
            {i18nT('apps.awsControl.console.backup_remembered', { count: remembered })}
          </button>
        )}
        {/* Two different failures share one line: a START the route refused
            (its thrown error rides along) and a RUN the server reports as its
            last outcome, whose reason arrives as text inside a 200 — so that
            one has only its sentence to hand over. */}
        {(failed || startError) && (
          <AwsErrorNotice
            askAgent
            error={startError ? runMut.error : undefined}
            message={startError || i18nT('apps.awsControl.console.backup_failed', { reason: failed?.error || '' })}
            variant="inline"
            testId={`backup-error-${kind}`}
          />
        )}
        {kind === 'sessions' && (
          // The archive takes BOTH halves of a session, and the CLI
          // half lives in a directory shared with any kiro-cli chat
          // started outside Kiro Crew. Say so where the button is:
          // the owner is choosing what leaves their machine.
          <div className="text-[12px] text-muted" data-testid="backup-sessions-scope">
            {i18nT('apps.awsControl.console.backup_sessions_scope')}
          </div>
        )}
      </div>
      {/* The badge states ONLY what the ledger records: a recorded run, or none.
          It is deliberately not a health verdict -- the ledger holds successes,
          so "a run exists" is the whole claim, and the failure line above is
          where a bad outcome is reported. A third state would be invented.

          Badge and Run travel together in one shrink-0 cluster, and the ROW
          wraps: at 320px the label + meta + badge + button do not fit on one
          line, and a non-wrapping row would push the button past the viewport
          edge instead of dropping the pair beneath. */}
      <div className="ml-auto flex shrink-0 items-center gap-2">
        <Badge variant={run ? 'ok' : 'muted'} data-testid={`backup-state-${kind}`}>
          {run
            ? i18nT('apps.awsControl.console.backup_state_current')
            : i18nT('apps.awsControl.console.backup_state_never')}
        </Badge>
        <Btn
          onClick={() => runMut.mutate()}
          disabled={busy || unsupported}
          data-testid={`backup-run-${kind}`}
        >
          <RefreshCw size={13} className={busy ? 'animate-spin' : ''} />
          {busy ? i18nT('apps.awsControl.console.backup_running') : i18nT('apps.awsControl.console.backup_run_now')}
        </Btn>
      </div>
    </div>
  )
}

/**
 * The attribution string shown beside an archive row.
 *
 * The label is resolved by looking the row's owning `install` id up in the
 * remote roster; a missing entry or an empty label falls back to the no-label
 * form. The id decides everything a restore is allowed to do -- this string is
 * only what a human reads.
 *
 *   self          <label> . this install
 *   other +label  "<label>" (another install . <first 8 hex>)
 *   other -label  another install . <first 8 hex>
 *   legacy        unknown origin
 *
 * The double quotes around a foreign label are required: they carry the meaning
 * "this string was written by whoever wrote the archive", not by us.
 */
function archiveAttribution(f: BackupArchive, remote: RemoteBackup | null): string {
  if (f.origin === 'legacy') return i18nT('apps.awsControl.console.backup_origin_unknown')
  // Under this install's own prefix, but this install has no record of uploading
  // it -- and a prefix is a folder name any bucket writer can create. Said plainly
  // rather than rendered as ownership.
  if (f.origin === 'unverified') return i18nT('apps.awsControl.console.backup_attribution_unverified')
  const label = remote?.installs.find((i) => i.id === f.install)?.label ?? ''
  // Each shape is ONE catalog key rather than pieces joined here. The separator,
  // the parentheses and the quotation marks are punctuation choices that differ by
  // locale -- assembling them in code would pin every language to English
  // typography, and the quote glyphs are exactly what carries "somebody else wrote
  // this string" to the reader.
  if (f.origin === 'self') {
    return label
      ? i18nT('apps.awsControl.console.backup_attribution_self', { label })
      : i18nT('apps.awsControl.console.backup_attribution_self_unnamed')
  }
  const id = f.install.slice(0, 8)
  return label
    ? i18nT('apps.awsControl.console.backup_attribution_other', { label, id })
    : i18nT('apps.awsControl.console.backup_attribution_other_unnamed', { id })
}

export function BackupSection({ account }: { account: string }) {
  const qc = useQueryClient()
  const appKey = useAppQueryKey()
  const [showRemote, setShowRemote] = useState(false)
  // The stored-archive disclosure the count line opens. Clicking that line far
  // up the pane used only to flip `showRemote`, which left the list it reveals
  // off-screen on a long pane -- the reader asked to see the archives and the
  // view did not move. This ref is the scroll target so the open and the scroll
  // happen together.
  const remoteRef = useRef<HTMLDivElement>(null)
  // A reveal is "pending" from the click until the paid remote rows actually
  // land. On the FIRST open, `setShowRemote(true)` changes the query key and
  // `placeholderData` keeps the prior response, which carries no `data.remote`,
  // so the panel renders at skeleton height; a scroll in that frame pins only
  // the toggle header and the rows that arrive later grow BELOW the viewport.
  // This flag lets the effect below re-scroll once those rows exist.
  const pendingRevealRef = useRef(false)
  // Opt-in: each other install listed costs extra paid AWS calls, so the drive
  // is asked about co-tenants only when the reader turns this on. Part of the
  // query key the same way `showRemote` is, so flipping it is a deliberate
  // refetch rather than a hidden cost on every poll.
  const [showOthers, setShowOthers] = useState(false)
  // `showRemote` and `showOthers` are part of the key on purpose: the remote
  // listing and the co-tenant roster each cost paid AWS calls, so they are
  // fetched only while the stored-archive list is open and only when asked.
  const backupQ = useAppQuery(['backup', account, showRemote, showOthers], {
    queryFn: () => awsControlApi.backup(account, { remote: showRemote, others: showOthers }),
    // The co-tenant toggle is part of the key, so without this the panel that
    // HOSTS the toggle unmounts to a skeleton for the round trip -- the control
    // the reader just clicked disappears under the cursor. Keyed on the account so
    // switching accounts still clears rather than showing the previous one's rows.
    //
    // Counted from the END of the resolved key, because the head of that key is
    // the host's to decide: the tail is this app's own three segments, so
    // `length - 3` names the account whether or not a namespace sits in front
    // of it. A fixed index from the front reads a toggle as the account on any
    // render that the host has not namespaced.
    placeholderData: (prev, prevQuery) => {
      const prevKey = prevQuery?.queryKey as unknown[] | undefined
      return prevKey?.[prevKey.length - 3] === account ? prev : undefined
    },
    // Poll only while a run is actually in flight: an idle section needs no
    // timer, and a start goes through `invalidate`, which is deterministic
    // rather than a wait for the next tick. A remount must ADOPT the server's
    // answer rather than render a cached idle state -- that cached-stale render
    // is the original bug wearing a different hat.
    refetchInterval: (query) =>
      BACKUP_KINDS.some((k) => query.state.data?.jobs?.[k]?.active != null) ? 3000 : false,
    staleTime: 0,
    refetchOnMount: 'always',
  })
  // Open the disclosure AND bring it into view. The scroll is deferred to the
  // next frame because `setShowRemote(true)` has to render the panel before
  // there is anything to scroll to; a `scrollIntoView` in the same tick would
  // run against the collapsed height. `block: 'start'` pins the wrapper's TOP
  // (not its current midpoint), so rows that load afterwards grow down into
  // view rather than past the bottom edge. Already-open is handled too: a
  // second click on the count line re-scrolls to the list rather than doing
  // nothing.
  //
  // The pending flag is set ONLY when the remote rows are not loaded yet, so
  // the `[data?.remote]` effect that clears it is guaranteed to fire and clear
  // it. If the list is already open (rows present), this reveal is a same-frame
  // re-scroll that needs no re-scroll-on-arrival, so leaving the flag unset
  // keeps it from leaking into a later refetch -- a poll, an `invalidate`, or a
  // co-tenant toggle flip -- that would otherwise yank the pane to the list
  // unasked. Declared after `backupQ` so reading `backupQ.data?.remote` here is
  // not a use-before-declaration.
  const revealRemote = useCallback(() => {
    setShowRemote(true)
    pendingRevealRef.current = !backupQ.data?.remote
    requestAnimationFrame(() => remoteRef.current?.scrollIntoView?.({ block: 'start', behavior: 'smooth' }))
  }, [backupQ.data?.remote])
  const invalidate = () => qc.invalidateQueries({ queryKey: appKey(['backup', account]) })
  const nightlyMut = useMutation({
    mutationFn: (enabled: boolean) => awsControlApi.backupNightly(account, enabled),
    onSuccess: invalidate,
  })
  // A separate mutation, not a parameter on the one above: the two grants are
  // separate requests to separate endpoints, and a failure to record one must
  // leave the other's switch exactly where it was.
  const nightlySessionsMut = useMutation({
    mutationFn: (enabled: boolean) => awsControlApi.backupNightlySessions(account, enabled),
    onSuccess: invalidate,
  })
  // The label decides only what a human reads; the restore gate never consults
  // it. On failure the label is left unchanged and the row says so.
  const [editingLabel, setEditingLabel] = useState(false)
  const [labelDraft, setLabelDraft] = useState('')
  const labelMut = useMutation({
    mutationFn: (label: string) => awsControlApi.installLabel(label),
    onSuccess: () => { setEditingLabel(false); invalidate() },
  })
  // The archive row awaiting an in-page confirm, by key. Never the label: two
  // installs can publish the same label, and the confirm is about a specific
  // object.
  const [confirmKey, setConfirmKey] = useState<string | null>(null)
  // Restore carries the confirmed override. A row whose origin is not 'self' asks
  // first, and confirming ANY of them sends foreignOk true -- foreign, unverified
  // and legacy alike -- because the backend refuses every origin it cannot prove is
  // this install's own.
  const restoreMut = useMutation({
    mutationFn: (v: { key: string; foreignOk?: boolean }) =>
      awsControlApi.backupRestore(account, v.key, v.foreignOk),
    onError: (error, v) => {
      // The refusal is the backend's own origin judgment, made against the
      // drive's current bytes; the row's rendered origin is a cached read of the
      // upload ledger. When the two disagree -- a 'self' row whose stored bytes a
      // co-writer has since replaced -- the unconfirmed restore lands here, and
      // without opening the confirm strip the refusal is a dead end: no path on
      // the page can ever send the override. Only an UNCONFIRMED attempt opens
      // the strip; a refusal of an attempt that already carried foreignOk stays
      // an error, because re-asking the question it answered would loop.
      if (!v.foreignOk && (error as Error | null)?.message === 'foreign_install_archive') {
        setConfirmKey(v.key)
      }
    },
  })

  const data = backupQ.data
  // The rows the archive list below actually renders -- each kind's listing
  // under its display cap, flattened in render order. Computed ONCE so the
  // empty state, the strip-visibility invariant, and the list itself share a
  // single definition of "on screen" and cannot drift when the cap changes.
  const renderedRows = BACKUP_KINDS.flatMap((kind) => (data?.remote?.[kind] ?? []).slice(0, 5))
  // The first reveal scrolls against skeleton height (see `revealRemote`): the
  // rows this reader clicked for only exist once `data.remote` resolves. When
  // they do, while a reveal is still pending, scroll once more so the grown
  // list lands in view, then clear the flag so later refetches do not yank the
  // pane around. Keyed on `data.remote` existing, which is exactly the fact the
  // placeholder response lacked on the first open.
  //
  // The remote read can also RESOLVE TO A FAILURE: the backend returns
  // `remote: null` with the reason in `remoteError`. That is still a resolved
  // response -- the click the reader made has been answered, and answered with
  // "no list to show" -- so the pending reveal is spent and must be dropped
  // WITHOUT scrolling. Leaving it set would let the next successful poll or
  // `invalidate` fire the scroll long after the click, yanking the pane to a
  // list the reader never saw appear. Keyed on `data.remoteError` as well so
  // this clear runs the moment the failure lands.
  useEffect(() => {
    if (!pendingRevealRef.current) return
    if (!data?.remote) {
      // A resolved failure (error present, no rows): cancel the pending reveal
      // silently. A placeholder/loading response carries neither, so this does
      // nothing until the fetch actually settles.
      if (data?.remoteError) pendingRevealRef.current = false
      return
    }
    pendingRevealRef.current = false
    requestAnimationFrame(() => remoteRef.current?.scrollIntoView?.({ block: 'start', behavior: 'smooth' }))
  }, [data?.remote, data?.remoteError])
  // The refusal sentence below and the confirm strip answer the same question,
  // so exactly one of them may be on screen -- and the strip only exists while
  // the disclosure is open AND its row is among the rendered rows. Visibility
  // is therefore DERIVED from the same rows the list renders: a state flag
  // would go stale when the disclosure collapses or a refetch drops the
  // refused key out of the listing, and the failure would render nowhere.
  // The strip must also be answering THIS refusal -- a strip opened for some
  // other row does not speak for the refused one.
  const refusedKey = restoreMut.variables?.key
  const refusalAnswered =
    confirmKey !== null &&
    confirmKey === refusedKey &&
    showRemote &&
    renderedRows.some((f) => f.key === confirmKey)
  // Which sentence a refusal gets follows the refused row's own drawn origin:
  // a 'self' row was refused because the drive's bytes moved under it, and the
  // foreign-install sentence would contradict the attribution the row itself
  // renders. Looked up across the whole listing (not the render slice) --
  // wording truth does not depend on the display cap. A row a refetch removed
  // entirely falls back to the foreign sentence, the pre-diff behavior.
  const refusedSelf = BACKUP_KINDS.some((kind) =>
    (data?.remote?.[kind] ?? []).some((f) => f.key === refusedKey && f.origin === 'self'))

  // Counted from the SAME rows the list below renders, so the empty state cannot
  // disagree with what is on screen: a kind holding only the label sidecar, or rows
  // trimmed by the per-kind cap, must not read as rows that exist.
  const rowCount = renderedRows.length

  return (
    <section data-testid="backup-section">
      <PaneHeader icon={<Archive size={18} />} title={i18nT('apps.awsControl.console.backup_title')} />
      {backupQ.isLoading && <ContentSkeleton rows={2} />}
      {/* A status read that fails must not leave the pane as a bare title — that
          reads as "no backups configured", the one thing it cannot mean. */}
      <AwsErrorNotice
        askAgent
        error={backupQ.error}
        message={backupQ.isError ? i18nT('apps.awsControl.console.backup_status_failed') : null}
        onRetry={() => backupQ.refetch()}
        testId="backup-status-error"
      />
      {data && (
        <Card className="px-2 py-3 md:px-3">
          <PanelSectionHeader
            label={i18nT('apps.awsControl.console.backup_title')}
            count={BACKUP_KINDS.length}
            className="mb-1 px-1"
          />
          <div className="divide-y divide-border">
          {BACKUP_KINDS.map((kind) => (
            <BackupRow
              key={kind}
              account={account}
              kind={kind}
              run={data.runs[kind]}
              // A floor on this kind's recorded archives, so the row can say the
              // single run line above it is not the whole list.
              remembered={data.rememberedArchives?.[kind]}
              // Account-scoped: this payload answers "is a backup running for THIS
              // account", which the app-scoped `_jobs/active` surface cannot.
              job={data.jobs?.[kind]}
              /* Only the sessions kind has a reported capability answer, and it is
                 host-wide rather than per-account, so it is the same answer the
                 manual run would give. */
              unsupported={kind === 'sessions' && data.nightlySessionsBlocked === 'host_unsupported'}
              // Re-read immediately after a start, rather than waiting out the
              // poll gap and looking like the click did nothing.
              onStarted={invalidate}
              // The same act as clicking the disclosure below: the reader asks for
              // the listing, which is why the paid remote half stays opt-in. It
              // also scrolls the list into view, since the count line can sit a
              // long way above the disclosure it opens.
              onShowArchives={revealRemote}
            />
          ))}
          <div className="flex items-center justify-between gap-3 px-3 py-2.5" data-testid="backup-nightly">
            <div className="min-w-0">
              <div className="text-[13px] font-medium text-text">{i18nT('apps.awsControl.console.backup_nightly')}</div>
              <div className="text-[12px] text-muted">{i18nT('apps.awsControl.console.backup_nightly_hint')}</div>
            </div>
            <Toggle checked={data.nightly} onChange={(v) => nightlyMut.mutate(v)} label={i18nT('apps.awsControl.console.backup_nightly')} />
          </div>
          {/* The toggle snaps back on a failed write; without a line under it the
              snap-back reads as a flaky control rather than a refused request.
              Directly under the row it explains, so the snap-back and its reason
              share one glance. */}
          {nightlyMut.isError && (
            <div className="px-3 py-2">
              <AwsErrorNotice
                askAgent
                error={nightlyMut.error}
                message={i18nT('apps.awsControl.console.backup_nightly_failed', {
                  setting: i18nT('apps.awsControl.console.backup_nightly'),
                })}
                testId="backup-nightly-error"
              />
            </div>
          )}
          {/* The transcript grant, and deliberately a SECOND switch rather than a
              wider meaning for the one above. The switch above was answered about
              memory; conversations are the most sensitive payload here, so the
              operator is asked separately and the answer defaults to off. The hint
              states what the archive carries, because an unattended upload the
              operator misunderstood is the failure that matters. */}
          <div className="flex items-center justify-between gap-3 px-3 py-2.5" data-testid="backup-nightly-sessions">
            <div className="min-w-0">
              <div className="text-[13px] font-medium text-text">{i18nT('apps.awsControl.console.backup_nightly_sessions')}</div>
              <div className="text-[12px] text-muted">
                {i18nT('apps.awsControl.console.backup_nightly_sessions_hint')}{' '}
                {/* The SAME string the archive row above carries, not a paraphrase
                    of it. This is the sentence the owner consents on, and the row
                    it schedules says the payload reaches past chats started here
                    into every session under this Kiro home. A hint that says less
                    than the row asks for a yes to more than it states, and two
                    wordings of one scope would drift; one string cannot. */}
                {i18nT('apps.awsControl.console.backup_sessions_scope')}{' '}
                {/* Where the bytes GO, which the two sentences above do not say. A
                    reader can understand the payload exactly and still decline for
                    want of a destination, so this names the storage and ties it to
                    the manual button they can already see. The button's own label is
                    interpolated rather than quoted, so renaming it carries through
                    here instead of leaving this sentence pointing at nothing. */}
                {i18nT('apps.awsControl.console.backup_nightly_sessions_destination', {
                  button: i18nT('apps.awsControl.console.backup_run_now'),
                })}
              </div>
            </div>
            <Toggle checked={data.nightlySessions === true} onChange={(v) => nightlySessionsMut.mutate(v)} label={i18nT('apps.awsControl.console.backup_nightly_sessions')} />
          </div>
          {/* Granted, and not running. Shown only in that combination: with the
              grant off there is nothing being withheld, and the hint above
              already says what the switch would do. The switch itself stays
              interactive and keeps reading back as the owner set it -- their
              answer is theirs -- while this says the schedule is not producing
              anything, which is the part they cannot otherwise see until the
              host is gone. */}
          {data.nightlySessions === true && data.nightlySessionsBlocked ? (
            <div className="px-3 pb-2 text-[12px] text-warn" data-testid="backup-nightly-sessions-blocked">
              {[
                nightlySessionsBlockedText(data.nightlySessionsBlocked),
                nightlySessionsBlockedNextText(data.nightlySessionsBlocked),
              ]
                .filter(Boolean)
                .join(' ')}
            </div>
          ) : null}
          {nightlySessionsMut.isError && (
            <div className="px-3 py-2">
              <AwsErrorNotice
                askAgent
                error={nightlySessionsMut.error}
                message={i18nT('apps.awsControl.console.backup_nightly_failed', {
                  setting: i18nT('apps.awsControl.console.backup_nightly_sessions'),
                })}
                testId="backup-nightly-sessions-error"
              />
            </div>
          )}
          {/* This install's own identity. The id decides what a restore is
              allowed to do; this LABEL is only what a human reads, so it is
              editable in place and its failure is stated without touching the
              stored name. */}
          <div className="flex flex-wrap items-center gap-3 px-3 py-2.5" data-testid="backup-install">
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium text-text">{i18nT('apps.awsControl.console.backup_install')}</div>
              {editingLabel ? (
                <div className="mt-1 flex flex-wrap items-center gap-2">
                  <Input
                    aria-label={i18nT('apps.awsControl.console.backup_install_name_label')}
                    value={labelDraft}
                    onChange={(e) => setLabelDraft(e.target.value)}
                    // Same keys as the three rename editors on this page. A reader
                    // habituated by those presses Enter here, and losing the name to
                    // a key that works everywhere else is the worst version of it.
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') labelMut.mutate(labelDraft)
                      if (e.key === 'Escape') { setEditingLabel(false); labelMut.reset() }
                    }}
                    autoFocus
                    disabled={labelMut.isPending}
                    data-testid="backup-install-name"
                    className="min-w-0 flex-1"
                  />
                  <Btn
                    primary
                    onClick={() => labelMut.mutate(labelDraft)}
                    disabled={labelMut.isPending}
                    data-testid="backup-install-save"
                  >
                    {i18nT('apps.awsControl.console.backup_install_save')}
                  </Btn>
                  <Btn
                    onClick={() => { setEditingLabel(false); labelMut.reset() }}
                    disabled={labelMut.isPending}
                    data-testid="backup-install-cancel"
                  >
                    {i18nT('apps.awsControl.console.backup_restore_confirm_no')}
                  </Btn>
                </div>
              ) : (
                <>
                  <div className="text-[12px]">
                    <span className="font-medium text-text" data-testid="backup-install-label">{data.install.label}</span>
                  </div>
                  {/* Its own line, like the nightly row directly above. Glued after
                      the label it wrapped badly at narrow widths, because a
                      renameable value of any length sat in front of a 20-word
                      sentence. */}
                  <div className="text-[12px] text-muted">
                    {i18nT('apps.awsControl.console.backup_install_hint')}
                  </div>
                </>
              )}
            </div>
            {!editingLabel && (
              <Btn
                className="ml-auto shrink-0"
                onClick={() => { setLabelDraft(data.install.label); setEditingLabel(true) }}
                data-testid="backup-install-rename"
              >
                <Pencil size={13} />{i18nT('apps.awsControl.console.backup_install_rename')}
              </Btn>
            )}
          </div>
          {labelMut.isError && (
            <div className="px-3 py-2">
              {/* No hand-off: this notice sits inside the open rename editor, whose
                  `labelDraft` is not saved anywhere durable -- the failed write is
                  the reason it is still only a draft. Handing off navigates to the
                  chat and unmounts this tree, so the name the reader just typed
                  would be gone when they came back. */}
              <AwsErrorNotice
                error={labelMut.error}
                message={i18nT('apps.awsControl.console.backup_install_rename_failed')}
                testId="backup-install-error"
              />
            </div>
          )}
          </div>
        </Card>
      )}

      {/* The remote half failed INSIDE a 200: the backend read the local ledger
          fine and reports why the archive listing did not come back. The reason
          is the message and the sentence is the lead, so the hand-off carries the
          text AWS actually returned rather than only "couldn't read". */}
      {data?.remoteError && (
        <AwsErrorNotice
          askAgent
          title={i18nT('apps.awsControl.console.backup_remote_error')}
          message={data.remoteError}
          className="mt-2"
          testId="backup-remote-error"
        />
      )}

      {/* Gated on status data existing, NOT on `data.remote`. The remote half is
        * opt-in behind `?remote=1`, which only this disclosure can request -- so
        * gating the disclosure on remote data made the control that enables
        * remote fetching wait for the fetch it enables, and the archive and
        * Restore became unreachable. The rows below are already null-safe. */}
      {data && (
        <div className="mt-2" ref={remoteRef}>
          <button
            onClick={() =>
              setShowRemote((v) => {
                // Collapsing cancels any reveal still waiting on remote rows:
                // without this, a pending flag set on open survives the collapse
                // and the next refetch would scroll the (reopened or still-mounted)
                // pane into view unasked.
                if (v) pendingRevealRef.current = false
                return !v
              })
            }
            className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
            aria-expanded={showRemote}
            data-testid="backup-remote-toggle"
          >
            {i18nT('apps.awsControl.console.backup_archive')}
            <ChevronDown size={12} className={`transition-transform ${showRemote ? 'rotate-180' : ''}`} />
          </button>
          {showRemote && (
            // One card: what FILTERS the list, what EXPLAINS the list, then the
            // list. The co-tenant count and the shared-drive note are the reason
            // foreign rows are in this list at all, so they belong with it rather
            // than stacked above it as loose paragraphs.
            <div className="mt-1.5 rounded-md border border-border bg-card" data-testid="backup-archive">
              {/* Opt-in: listing co-tenant installs costs extra paid AWS calls,
                  so it is off until asked. Part of the query key, so flipping it
                  refetches. */}
              <div
                className="flex items-center justify-between gap-3 border-b border-border px-3 py-2.5"
                data-testid="backup-others-toggle"
              >
                <div className="min-w-0">
                  <div className="text-[13px] font-medium text-text">
                    {i18nT('apps.awsControl.console.backup_show_others')}
                  </div>
                  {data.remote && data.remote.others > 0 && (
                    <div className="text-[12px] text-muted" data-testid="backup-others-on-drive">
                      {i18nT('apps.awsControl.console.backup_others_on_drive', { count: data.remote.others })}
                    </div>
                  )}
                </div>
                <Toggle
                  checked={showOthers}
                  onChange={(v) => setShowOthers(v)}
                  label={i18nT('apps.awsControl.console.backup_show_others')}
                />
              </div>
              {data.remote && data.remote.others > 0 && (
                <p
                  className="border-b border-border px-3 py-2 text-[12px] text-muted"
                  data-testid="backup-shared-note"
                >
                  {i18nT('apps.awsControl.console.backup_shared_note')}
                </p>
              )}
              {data.remote?.truncated && (
                <p
                  className="border-b border-border px-3 py-2 text-[12px] text-muted"
                  data-testid="backup-others-truncated"
                >
                  {i18nT('apps.awsControl.console.backup_others_truncated', { max: data.remote.max })}
                </p>
              )}
              <div className="divide-y divide-border">
                {/* A fresh install is the case this whole feature exists for -- its
                    predecessor's archives are the only copy left -- and it is
                    exactly the case with no rows of its own. Without this line the
                    card renders a co-tenant count above an empty list, and nothing
                    tells the reader that the toggle is what reveals the archives the
                    count is counting. Only while the toggle is OFF: once it is on,
                    an instruction to turn it on is not an instruction. */}
                {rowCount === 0 && !showOthers && (data.remote?.others ?? 0) > 0 && (
                  <p className="px-3 py-2 text-[12px] text-muted" data-testid="backup-no-own-archives">
                    {i18nT('apps.awsControl.console.backup_none_from_this_install')}
                  </p>
                )}
                {renderedRows.map((f) => {
                  const asking = confirmKey === f.key
                  return (
                    <div key={f.key} data-testid="backup-archive-row">
                      <div className="flex flex-wrap items-center gap-2 px-3 py-2 text-[12px]">
                        <span className="min-w-0 flex-1 truncate font-mono text-text" data-testid="backup-archive-key">{f.key}</span>
                        <span className="basis-full shrink-0 text-muted sm:basis-auto" data-testid="backup-archive-origin">
                          {archiveAttribution(f, data.remote)}
                        </span>
                        <span className="hidden shrink-0 text-muted sm:inline">{fmtBytes(f.size)}</span>
                        <Btn
                          onClick={() => {
                            // 'self' restores at once; anything else asks first.
                            if (f.origin === 'self') restoreMut.mutate({ key: f.key })
                            else setConfirmKey(f.key)
                          }}
                          disabled={restoreMut.isPending}
                          data-testid="backup-restore"
                        >
                          <Download size={13} />{i18nT('apps.awsControl.console.backup_restore')}
                        </Btn>
                      </div>
                      {asking && (
                        <div className="flex flex-wrap items-center gap-2 px-3 pb-2.5" data-testid="backup-restore-confirm">
                          <p className="basis-full text-[12px] text-muted">
                            {f.origin === 'legacy'
                              ? i18nT('apps.awsControl.console.backup_restore_unknown_confirm')
                              : f.origin === 'unverified'
                                ? i18nT('apps.awsControl.console.backup_restore_unverified_confirm')
                                : f.origin === 'self'
                                  // A self row reaches this strip only after the
                                  // backend refused its unconfirmed restore: the
                                  // ledger says this install uploaded it, the
                                  // drive's bytes no longer match that record.
                                  // Neither the foreign nor the unverified
                                  // sentence is true of that state, so it gets
                                  // its own.
                                  ? i18nT('apps.awsControl.console.backup_restore_overwritten_confirm')
                                  : i18nT('apps.awsControl.console.backup_restore_foreign_confirm', { install: f.install.slice(0, 8) })}
                          </p>
                          <Btn
                            onClick={() => {
                              // The backend refuses ANY origin it cannot prove is
                              // this install's own, so confirming any of the three
                              // cases sends the override. It is not a "foreign
                              // only" flag: it means the reader accepts that this
                              // archive may not be this machine's.
                              restoreMut.mutate({ key: f.key, foreignOk: true })
                              setConfirmKey(null)
                            }}
                            disabled={restoreMut.isPending}
                            data-testid="backup-restore-confirm-yes"
                          >
                            {i18nT('apps.awsControl.console.backup_restore_confirm_yes')}
                          </Btn>
                          <Btn
                            onClick={() => {
                              // Cancel CONCLUDES a refused attempt: the reader
                              // answered the question with "no", so the refusal
                              // sentence must not come back and tell them to
                              // answer it again. Only this strip's own refusal
                              // is cleared -- a pre-flight confirm fired no
                              // attempt, and an unrelated earlier failure keeps
                              // its sentence.
                              if (
                                f.key === restoreMut.variables?.key &&
                                restoreMut.isError &&
                                (restoreMut.error as Error | null)?.message === 'foreign_install_archive'
                              ) {
                                restoreMut.reset()
                              }
                              setConfirmKey(null)
                            }}
                            data-testid="backup-restore-confirm-no"
                          >
                            {i18nT('apps.awsControl.console.backup_restore_confirm_no')}
                          </Btn>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}
          {showRemote && (
            // The recommended least-privilege policy makes the backup prefix
            // write-only on purpose, so Restore is denied for anyone who pasted
            // exactly that tier. Say so where the button is instead of letting
            // them discover it as an AccessDenied.
            <p className="mt-1.5 text-[12px] text-muted" data-testid="backup-restore-caveat">
              {i18nT('apps.awsControl.console.backup_restore_caveat')}
            </p>
          )}
        </div>
      )}

      {/* Restore is the call the recommended write-only policy denies (the
          caveat above says so) — and its failure used to be invisible, the one
          outcome that caveat exists to explain. A `foreign_install_archive`
          refusal is its own sentence, keyed to the refused row's drawn origin —
          but only while the strip answering THIS refusal is actually on screen.
          The strip IS the confirmation the sentence asks for, so rendering both
          would tell the reader to do the thing the page is already asking them
          to do; the moment the strip is not rendered (disclosure collapsed, row
          out of the render slice, or the strip belongs to another row), the
          sentence carries the failure instead. */}
      <AwsErrorNotice
        askAgent
        error={restoreMut.error}
        message={
          restoreMut.isError
            ? (restoreMut.error as Error | null)?.message === 'foreign_install_archive'
              ? refusalAnswered
                ? null
                : i18nT(refusedSelf
                  ? 'apps.awsControl.console.backup_restore_overwritten_refused'
                  : 'apps.awsControl.console.backup_restore_foreign_refused')
              : i18nT('apps.awsControl.console.backup_restore_failed')
            : null
        }
        className="mt-2"
        testId="backup-restore-error"
      />

      {restoreMut.data && (
        <div className="mt-2 rounded-md border border-border bg-bg-elevated p-2.5 text-[12px]" data-testid="backup-restored">
          {/* First and WARN-toned: in the same muted style as the note below it,
            * this reads as one more line of a normal restore and gets skimmed.
            * Above the note, so "the path below" there still points at the path. */}
          {restoreMut.data.recovered && (
            <p className="mb-2 flex items-start gap-1.5 text-warn" data-testid="backup-restore-recovered">
              <AlertTriangle className="lucide-inline" />
              <span>{i18nT('apps.awsControl.console.backup_restore_recovered_note')}</span>
            </p>
          )}
          <div className="mb-1 text-muted">{i18nT('apps.awsControl.console.backup_restored_note')}</div>
          <div className="flex items-center gap-2">
            <code className="flex-1 min-w-0 break-all rounded bg-bg px-2 py-1.5 font-mono text-[12px] text-text">{restoreMut.data.path}</code>
            <CopyBtn text={restoreMut.data.path} />
          </div>
        </div>
      )}
    </section>
  )
}
