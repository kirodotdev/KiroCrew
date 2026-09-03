import { useEffect, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Trans } from 'react-i18next'
import MarkdownRenderer from './MarkdownRenderer'
import { Download, X, Copy, Check } from 'lucide-react'

import { api, ApiError } from '../api/client'
import { useAppSelector } from '../store'
import { i18nT } from '../i18n/t'
import { copyToClipboard } from '../utils/clipboard'
import { updateAffordance } from '../utils/updateAffordance'
import { settingsPath } from './settingsPath'
import { shouldNudge, snoozeRecord, skipRecord, type UpdateNudgeRecord } from '../utils/updateNudge'
import { foldStableStamp } from '../utils/displayVersion'
import type { UpdateState } from '../hooks/useUpdateSubscription'

/**
 * Proactive "update found" modal — the one interruption between an update
 * being discovered and the user deciding what to do with it.
 *
 * It fills the gap the passive surfaces leave: the nav dot and the top-bar
 * pill say an update exists, this modal says it ONCE, with the version, the
 * notes, and an explicit action. Interruption policy lives in
 * `utils/updateNudge` and is per-version: "Remind me tomorrow" (also what a
 * plain dismissal writes, so closing the modal never means being re-asked on
 * the next reload) and "Skip this version" both persist to gateway config,
 * so the decision holds across browsers and the desktop app.
 *
 * Two sources feed one surface:
 * - Desktop (Electron): a live `found`/`available` payload in the shared
 *   ['update-state'] cache. Replayed payloads never open it — same contract
 *   as UpdateModal. Primary action = consent to download; progress then
 *   lives on the top-bar pill and UpdateModal takes over at `downloaded`.
 * - Gateway: `update_available === true` on the status frame. The primary
 *   action follows `updateAffordance`: in-process apply where the install
 *   supports it, a copyable installer command where it does not. An install
 *   with no affordance at all is never interrupted.
 *
 * Download consent stays with the user in every path: nothing downloads or
 * installs from merely showing this modal.
 */

type UpdateAPI = { download?: () => Promise<unknown> }

function getUpdateApi(): UpdateAPI | undefined {
  return (window as unknown as { updateAPI?: UpdateAPI }).updateAPI
}

/**
 * Can this renderer actually start the download? An older preload paired with
 * a newer renderer may expose updateAPI without `download` (the same skew
 * useUpdateSubscription documents for getInfo) — a popup whose primary button
 * silently no-ops is worse than no popup, so such an install is treated like
 * a no-affordance gateway install and never interrupted.
 */
function desktopCanDownload(): boolean {
  return typeof getUpdateApi()?.download === 'function'
}

type Candidate = {
  source: 'desktop' | 'gateway'
  version: string
  /** What the popup PRINTS — the promoted-stamp fold of `version` on the
   *  stable channel. `version` itself stays raw: it keys the per-version
   *  snooze/skip records, and a fold there would make dismissing `0.4.0`
   *  swallow the next release's `0.4.0rcN` candidate too. */
  displayVersion: string
  notes?: string
  /** Gateway only: which action the primary slot offers. */
  affordance?: 'apply' | 'command'
  command?: string
}

export default function UpdateFoundModal() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const { data: desktop } = useQuery<UpdateState | null>({
    queryKey: ['update-state'],
    queryFn: () => null,
    enabled: false, // populated by useUpdateSubscription (App.tsx)
    staleTime: Infinity,
  })

  const gwAvailable = useAppSelector(s => s.dashboard.status?.update_available === true)
  const gwVersion = useAppSelector(s => s.dashboard.status?.update_latest_version) || ''
  // Backend-folded sibling for DISPLAY (clean base on the stable channel).
  // Raw fallback below covers a gateway that predates the field.
  const gwVersionDisplay = useAppSelector(s => s.dashboard.status?.update_latest_version_display) || ''
  const gwCanApply = useAppSelector(s => s.dashboard.status?.update_can_apply)
  const gwCommand = useAppSelector(s => s.dashboard.status?.update_command) || ''
  const gwRequired = useAppSelector(s => s.dashboard.status?.update_required === true)
  const gwMinVersion = useAppSelector(s => s.dashboard.status?.update_min_version) || ''

  // When the user enabled background auto-download, the Electron main process
  // starts the download itself right after `found` — a consent popup whose
  // "nothing downloads until you choose" line is false for the frame it
  // flashes. Those users chose the quiet flow: the staged-build modal at
  // `downloaded` is their prompt. Absent/older bridges report no preference
  // and keep the popup.
  const { data: bridgeInfo } = useQuery({
    queryKey: ['update-info'],
    queryFn: async () =>
      (window as unknown as { updateAPI?: { getInfo?: () => Promise<{ autoDownload?: boolean; channel?: string | null }> } })
        .updateAPI?.getInfo?.() ?? null,
    enabled: !!desktop && (desktop.state === 'found' || desktop.state === 'available'),
    staleTime: Infinity,
  })

  // Desktop first: when the dashboard runs inside the desktop app the gateway
  // defers its own check (`managed_by_app`), so the two cannot both report —
  // this ordering is belt-and-braces for the transition frame, not a policy.
  // `bridgeInfo === undefined` means the preference read is still in flight:
  // candidacy waits for it, otherwise the popup opens for a frame and then
  // vanishes when an auto-download preference lands.
  let candidate: Candidate | null = null
  if (desktop && (desktop.state === 'found' || desktop.state === 'available') && !desktop.replayed && desktop.version && desktopCanDownload() && bridgeInfo !== undefined && bridgeInfo?.autoDownload !== true) {
    // Desktop-reported version never crosses the gateway, so the fold is
    // computed locally, keyed on the followed channel like the backend's.
    candidate = {
      source: 'desktop', version: desktop.version,
      displayVersion: foldStableStamp(desktop.version, bridgeInfo?.channel),
      notes: desktop.notes,
    }
  } else if (gwAvailable && gwVersion) {
    const afford = updateAffordance({ updateAvailable: true, canApply: gwCanApply, command: gwCommand })
    if (afford !== 'none') {
      candidate = {
        source: 'gateway', version: gwVersion,
        displayVersion: gwVersionDisplay || gwVersion,
        affordance: afford, command: gwCommand,
      }
    }
  }

  // The persisted per-version verdict. Only fetched once a candidate exists,
  // so the browser-idle path costs nothing.
  const { data: record, isSuccess: recordLoaded } = useQuery<UpdateNudgeRecord>({
    queryKey: ['mc-config-update-nudge'],
    queryFn: async () => {
      const cfg = await api.kirocrewConfig() as { dashboard?: { update_nudge?: UpdateNudgeRecord } }
      return cfg?.dashboard?.update_nudge ?? {}
    },
    enabled: !!candidate,
    staleTime: 60_000,
  })

  // Keyed to the version, not a boolean: dismissing 0.5.0 must not consume
  // the one proactive prompt 0.6.0 is owed when it arrives in this same
  // long-lived window (the desktop app's normal lifetime is days).
  const [dismissedVersion, setDismissedVersion] = useState('')
  const sessionDismissed = !!candidate && dismissedVersion === candidate.version
  const [copied, setCopied] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [applyError, setApplyError] = useState('')
  const [persistError, setPersistError] = useState('')

  // Mandatory update (below the governance pin or the release feed's
  // breaking-change floor): the modal loses every dismissal affordance —
  // no X, no Escape, no backdrop click, no snooze/skip — and opens without
  // waiting for the per-version verdict record, because no verdict can
  // apply. It never blocks the gateway itself; the enforcement surface is
  // this prompt staying up.
  const required = !!candidate && gwRequired

  // Waiting for the record before opening is what makes "skip 0.5.0" hold
  // across reloads: opening optimistically would flash the modal at every
  // user who already skipped.
  const open = !!candidate
    && (required || (
      recordLoaded
      && !sessionDismissed
      && shouldNudge(candidate.version, record, Date.now() / 1000)
    ))

  // Gateway notes are fetched only when the modal actually opens: the status
  // frame deliberately omits the changelog blob, and GET /api/update/check
  // runs a real check per call — affordable once per shown version, not per
  // status tick.
  const { data: gwCheck } = useQuery({
    queryKey: ['update-found-notes', candidate?.version ?? ''],
    queryFn: () => api.checkUpdate(),
    enabled: open && candidate?.source === 'gateway',
    staleTime: Infinity,
  })

  const persist = useMutation({
    mutationFn: async (rec: Required<UpdateNudgeRecord>) => {
      // ONE atomic PATCH of the whole record. Per-field writes would open a
      // crash window (old verdict paired with a new version) and let two
      // concurrent dashboards interleave fields into a verdict nobody
      // expressed — the record only means anything as a unit.
      await api.patchConfig('dashboard.update_nudge', rec)
      return rec
    },
    // Mirror the write into the cache: the record query's staleTime means a
    // later candidate in this same page session would otherwise read the
    // pre-snooze record. Cancel any in-flight refetch first — a focus-refetch
    // started before the click would otherwise resolve after this write and
    // put the pre-verdict record back.
    onSuccess: async (rec) => {
      await queryClient.cancelQueries({ queryKey: ['mc-config-update-nudge'] })
      queryClient.setQueryData(['mc-config-update-nudge'], rec)
    },
  })

  const gwApply = useMutation({
    mutationFn: () => api.applyUpdate(),
    onSuccess: () => setRestarting(true),
    onError: (e: unknown) => {
      // Same contract as the About panel's apply: a bare network failure is
      // the gateway restarting out from under the POST — the success path —
      // and only a real server rejection (ApiError) is worth surfacing.
      if (e instanceof ApiError) setApplyError(e.message || i18nT('components.updateFoundModal.update_failed'))
      else setRestarting(true)
    },
  })

  // Any dismissal is a snooze, never a plain close: an un-persisted dismiss
  // would re-interrupt on the next reload, and nagging is the one behaviour
  // this modal must not have. The close is gated on the PATCH succeeding —
  // closing optimistically would silently discard a failed write and produce
  // exactly that re-nag, with the user believing they answered.
  // Synchronous in-flight latch. `persist.isPending` only updates on the
  // NEXT render, so a handler whose closure predates the click (the window
  // Escape listener) still reads false while a PATCH is in flight — letting
  // Escape overwrite a pending skip with a snooze. A ref is immune to stale
  // closures: it is set before mutate() returns and read at call time.
  const persistInFlight = useRef(false)
  const verdict = (rec: Required<UpdateNudgeRecord>) => {
    if (!candidate || restarting || persistInFlight.current) return
    // After a write already failed, dismissal degrades to session-only: the
    // user has SEEN the couldn't-save note, so closing without persistence is
    // informed — while re-gating every close on a write that keeps failing
    // holds a full-screen modal hostage over a config file.
    if (persistError) {
      setDismissedVersion(rec.version)
      return
    }
    persistInFlight.current = true
    persist.mutate(rec, {
      onSuccess: () => setDismissedVersion(rec.version),
      onError: () => setPersistError(i18nT('components.updateFoundModal.could_not_save_choice')),
      onSettled: () => { persistInFlight.current = false },
    })
  }
  const dismiss = () => { if (candidate && !required) verdict(snoozeRecord(candidate.version, Date.now() / 1000)) }
  const skip = () => { if (candidate && !required) verdict(skipRecord(candidate.version)) }

  useEffect(() => {
    if (!open || required) return
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') dismiss() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, required, candidate?.version, restarting])

  // Move focus INTO the dialog when it opens: aria-modal hides the background
  // from assistive tech, so focus stranded out there points at content the
  // screen reader was just told does not exist.
  useEffect(() => {
    if (open) dialogRef.current?.focus()
  }, [open])

  // A save failure is a verdict on ONE version's write, not a permanent
  // downgrade to session-only dismissal: left sticky, the NEXT release's
  // snooze/skip would silently bypass persistence and be lost on reload.
  const candidateVersion = candidate?.version ?? ''
  useEffect(() => {
    setPersistError('')
  }, [candidateVersion])

  // "Copied" is transient feedback, not a latch.
  useEffect(() => {
    if (!copied) return
    const t = setTimeout(() => setCopied(false), 2000)
    return () => clearTimeout(t)
  }, [copied])

  if (!open || !candidate) return null

  const notes = (candidate.source === 'gateway' ? gwCheck?.changes : candidate.notes)?.trim() || ''

  const primary = () => {
    if (candidate.source === 'desktop') {
      // Consent to download. Progress rides the shared ['update-state'] cache
      // (top-bar pill), and the existing UpdateModal interrupts at
      // `downloaded` — the state change also closes this modal by itself.
      getUpdateApi()?.download?.()
      setDismissedVersion(candidate.version)
    } else if (candidate.affordance === 'apply') {
      setApplyError('')
      gwApply.mutate()
    }
  }

  const goToAbout = () => {
    dismiss()
    navigate(settingsPath({ tab: 'about' }))
  }

  return (
    // The scrim is presentation, not a control: an ARIA button must not
    // contain interactive descendants, and with programmatic focus never
    // landing here a scrim keydown handler is unreachable anyway. Click-to-
    // dismiss needs no role; Escape covers keyboard dismissal.
    <div
      className="fixed inset-0 z-50 bg-bg/80 backdrop-blur-sm flex items-center justify-center animate-rise"
      role="presentation"
      onClick={e => { if (e.target === e.currentTarget && !required) dismiss() }}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="bg-card border border-border rounded-xl shadow-xl w-[460px] max-w-[90vw] flex flex-col overflow-hidden outline-none"
        role="dialog"
        aria-modal="true"
        aria-label={required
          ? i18nT('components.updateFoundModal.update_required')
          : i18nT('components.updateFoundModal.update_available')}
      >
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-border bg-bg-elevated">
          <div className="flex items-center gap-2">
            <Download className="lucide-inline text-accent" size={16} />
            <span className="text-sm font-semibold text-text">
              {required
                ? i18nT('components.updateFoundModal.update_required')
                : i18nT('components.updateFoundModal.update_available')}
            </span>
          </div>
          {!required && (
            <button
              type="button"
              className="text-muted hover:text-text cursor-pointer bg-transparent border-none disabled:opacity-50"
              onClick={dismiss}
              disabled={restarting || persist.isPending}
              aria-label={i18nT('components.updateFoundModal.dismiss')}
            >
              <X size={16} />
            </button>
          )}
        </div>

        <div className="px-4 py-3 text-sm text-text">
          <p>
            {/* One interpolated key, not fixed JSX fragments: the version slot
                sits in locale-specific positions (sentence-medial in ja/ko,
                after the noun in ru), which hard-coded fragment order and
                ASCII joiner spaces cannot express. The bold wrapper is the
                NUMBERED tag <0> because the catalog integrity gate forbids
                letter-named closing tags in values. */}
            <Trans
              i18nKey="components.updateFoundModal.version_is_available"
              values={{ version: candidate.displayVersion }}
              components={[<span key="v" className="font-semibold" />]}
            />
          </p>
          {required && (
            // Why the modal cannot be dismissed. `update_min_version` is
            // non-empty whenever `update_required` is true (both authorities
            // return a floor or return not-required), so the note always
            // names the floor.
            <p data-testid="update-required-note" className="mt-2 text-[12px] text-danger">
              {i18nT('components.updateFoundModal.this_version_is_below_minimum', { version: gwMinVersion })}
            </p>
          )}
          {notes && (
            // MarkdownRenderer, matching Settings › About's changelog box:
            // gateway notes come straight from CHANGELOG.md — headers, bold,
            // nested lists — and a real release runs dozens of lines, so a
            // plain pre-wrap paragraph would print raw markdown syntax.
            <div className="mt-2 p-3 bg-bg rounded-lg border border-border max-h-56 overflow-y-auto text-[13px] text-muted">
              <MarkdownRenderer content={notes} />
            </div>
          )}
          {candidate.source === 'desktop' && (
            <p className="mt-2 text-[12px] text-muted">
              {i18nT('components.updateFoundModal.nothing_downloads_until_you_choose_to')}
            </p>
          )}
          {candidate.source === 'gateway' && candidate.affordance === 'command' && (
            <code data-testid="update-found-command" className="block mt-2 text-[12px] bg-bg border border-border rounded-md px-2 py-1.5 overflow-x-auto whitespace-nowrap">{candidate.command}</code>
          )}
          {applyError && <p className="mt-2 text-[12px] text-danger">{applyError}</p>}
          {required && applyError && candidate.affordance === 'apply' && gwCommand && (
            // Escape hatch for the worst state: a mandatory update whose
            // in-process apply keeps failing would otherwise strand the user
            // behind this modal with only a retry button. The installer
            // command (already on the status frame) is the way out via a
            // terminal; the modal clears on its own once the gateway
            // restarts on the new version.
            <code data-testid="update-required-fallback-command" className="block mt-2 text-[12px] bg-bg border border-border rounded-md px-2 py-1.5 overflow-x-auto whitespace-nowrap">{gwCommand}</code>
          )}
          {persistError && <p role="alert" className="mt-2 text-[12px] text-danger">{persistError}</p>}
          {restarting && <p className="mt-2 text-[12px] text-muted">{i18nT('components.updateFoundModal.updating_and_restarting')}</p>}
          {!required && (
          <p className="mt-2 text-[12px] flex items-center gap-3">
            {/* One full-sentence key, not a prefix + link-text pair: the link
                phrase sits sentence-finally in ja/ko, so a split key cannot be
                translated with correct word order. */}
            <button
              type="button"
              className="underline text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 text-[12px] text-left"
              onClick={goToAbout}
            >
              {i18nT('components.updateFoundModal.you_can_update_anytime_from_settings_about')}
            </button>
            {/* Skip lives with the low-emphasis prose, keeping the footer at
                its two-action maximum — a three-button footer row makes the
                dismissal read as the primary choice. */}
            <button
              type="button"
              className="underline text-muted hover:text-text bg-transparent border-none p-0 text-[12px] cursor-pointer disabled:opacity-50 shrink-0"
              onClick={skip}
              disabled={restarting || persist.isPending}
            >
              {i18nT('components.updateFoundModal.skip_this_version')}
            </button>
          </p>
          )}
        </div>

        {/* flex-wrap: two localized buttons (de runs long) can exceed a 320px
            footer's content box; wrapping stacks them instead of clipping the
            leading action. */}
        <div className="flex flex-wrap items-center justify-end gap-2 px-4 py-2.5 border-t border-border bg-bg-elevated">
          {!required && (
          <button
            type="button"
            className="px-3 py-1.5 text-sm rounded-md border border-border text-text hover:border-border-strong bg-transparent cursor-pointer disabled:opacity-50"
            onClick={dismiss}
            disabled={restarting || persist.isPending}
          >
            {i18nT('components.updateFoundModal.remind_me_tomorrow')}
          </button>
          )}
          {candidate.source === 'gateway' && candidate.affordance === 'command' ? (
            <button
              type="button"
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-accent text-accent-fg hover:opacity-90 cursor-pointer"
              onClick={async () => { await copyToClipboard(candidate.command || ''); setCopied(true) }}
            >
              {copied ? <Check size={14} className="lucide-inline" /> : <Copy size={14} className="lucide-inline" />}
              {copied ? i18nT('components.updateFoundModal.copied') : i18nT('components.updateFoundModal.copy_command')}
            </button>
          ) : (
              <button
                type="button"
                className="px-3 py-1.5 text-sm rounded-md bg-accent text-accent-fg hover:opacity-90 cursor-pointer disabled:opacity-50"
                onClick={primary}
                disabled={gwApply.isPending || restarting}
              >
                {candidate.source === 'desktop'
                  ? i18nT('components.updateFoundModal.download')
                  : restarting || gwApply.isPending
                    ? i18nT('components.updateFoundModal.updating')
                    : i18nT('components.updateFoundModal.update_now')}
              </button>
            )}
        </div>
      </div>
    </div>
  )
}
