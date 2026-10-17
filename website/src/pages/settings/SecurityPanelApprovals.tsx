/**
 * Settings -> Security -> Auto-approve and Flagged-file delivery: the two owner
 * grants that relax a default for a bounded time, the ad-hoc auto-approve
 * duration and the flagged-file delivery consent. One of the sections
 * `SecurityPanel.tsx` composes.
 */
import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Lock, CheckCircle2, Circle, Clock, Copy, Check } from 'lucide-react'
import { useAppDispatch, useAppSelector } from '../../store'
import { setYoloDuration } from '../../store/dashboardSlice'
import { Badge, Btn } from '../../components/ui'
import { SettingsCard } from '../../components/settings'
import {
  api, type ArmedFileDeliveryConsent, type FileDeliveryConsentStatus,
} from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtDateTime, fmtDuration, fmtTimeNumeric } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'
import { copyToClipboard } from '../../utils/clipboard'

/* ── Ad-hoc auto-approve duration ── */

export interface KirocrewCfgShape { agent?: { yolo_duration?: string; apps_allow_third_party?: unknown } }

const YOLO_DURATION_KEYS = ['30m', '1h', '6h', '12h', '24h', 'until_shutdown'] as const
type YoloDurationKey = (typeof YOLO_DURATION_KEYS)[number]

/** How long auto-approve lasts when it is turned on AD HOC — from the dashboard
 *  picker, Slack, or the API. All of those share this one value; the separate
 *  per-surface timers are gone. `until_shutdown` is disabled + lock-badged when
 *  an enterprise policy forbids it (status.yolo_until_shutdown_permitted ===
 *  false), the same ceiling the backend clamps at the source.
 *
 *  Deliberately does NOT expose the never-expiring DECLARED grant
 *  (agent.dangerouslySkipPermissions) — that stays config-file-only. */
export function YoloDurationCard() {
  const qc = useQueryClient()
  const dispatch = useAppDispatch()
  const status = useAppSelector(s => s.dashboard.status)
  const untilShutdownPermitted = status?.yolo_until_shutdown_permitted ?? true
  const { data, isError: cfgError } = useQuery<KirocrewCfgShape>({ queryKey: ['kirocrewConfig'], queryFn: api.kirocrewConfig })
  const configured = data?.agent?.yolo_duration
  const current: YoloDurationKey =
    YOLO_DURATION_KEYS.find(k => k === configured) ?? '6h'
  const save = useMutation({
    mutationFn: (v: YoloDurationKey) => api.patchConfig('agent.yolo_duration', v),
    onSuccess: (_data, saved) => {
      qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      // The approval-mode picker reads the duration from `dashboard.status`,
      // which only the HTTP status reply carries (the WebSocket push omits it).
      // Record the save in the store, or the picker keeps naming the duration
      // from page load until a reload while the gateway already grants this one.
      dispatch(setYoloDuration(saved))
    },
  })

  // Live "when does this end" line, so a no-expiry grant is never mistaken for
  // a bounded one.
  let activeNote: string | null = null
  if (status?.yolo) {
    if (status.yolo_until_shutdown) {
      activeNote = i18nT('pages.settings.securityPanel.yolo_active_until_restart')
    } else if (status.yolo_expires_at) {
      activeNote = i18nT('pages.settings.securityPanel.yolo_active_expires_at', {
        time: fmtTimeNumeric(status.yolo_expires_at),
      })
    }
  }

  function optionLabel(k: YoloDurationKey): string {
    switch (k) {
      case '30m': return i18nT('pages.settings.securityPanel.yolo_duration_30m')
      case '1h': return i18nT('pages.settings.securityPanel.yolo_duration_1h')
      case '6h': return i18nT('pages.settings.securityPanel.yolo_duration_6h')
      case '12h': return i18nT('pages.settings.securityPanel.yolo_duration_12h')
      case '24h': return i18nT('pages.settings.securityPanel.yolo_duration_24h')
      default: return i18nT('pages.settings.securityPanel.yolo_duration_until_shutdown')
    }
  }

  return (
    <SettingsCard>
      <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.yolo_duration_title')}</div>
      <div className="text-[12px] text-muted mt-0.5 mb-2 leading-relaxed">{i18nT('pages.settings.securityPanel.yolo_duration_desc')}</div>
      {activeNote && (
        <div className="text-[12px] text-accent mb-2 flex items-center gap-1">
          <Clock size={12} className="shrink-0" />{activeNote}
        </div>
      )}
      {/* data-setting-label: deep-link anchor for the manual registry entry
          (settingsManual.ts) — the highlight hook queries the rendered label. */}
      <div className="flex flex-col gap-1.5" role="radiogroup" aria-label={i18nT('pages.settings.securityPanel.yolo_duration_title')} data-setting-label={i18nT('pages.settings.securityPanel.yolo_duration_title')}>
        {YOLO_DURATION_KEYS.map(k => {
          const selected = current === k
          const disabled = k === 'until_shutdown' && !untilShutdownPermitted
          return (
            <button
              key={k}
              type="button"
              role="radio"
              aria-checked={selected}
              disabled={disabled || save.isPending}
              onClick={() => { if (!disabled && !selected) save.mutate(k) }}
              className={`flex items-center gap-2.5 text-left rounded-md border px-3 py-2 transition-colors bg-transparent cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${selected ? 'border-accent bg-accent-subtle' : 'border-border hover:bg-bg-hover'}`}
            >
              <span className="shrink-0">
                {selected ? <CheckCircle2 size={14} className="text-accent" /> : <Circle size={14} className="text-muted" />}
              </span>
              <span className="text-[12px] text-text flex-1">{optionLabel(k)}</span>
              {disabled && (
                <span className="text-[11px] text-muted flex items-center gap-1">
                  <Lock size={11} className="shrink-0" />
                  {i18nT('pages.settings.securityPanel.yolo_duration_locked')}
                </span>
              )}
            </button>
          )
        })}
      </div>
      <div className="text-[11px] text-muted mt-2">{i18nT('pages.settings.securityPanel.yolo_duration_next_activation_note')}</div>
      {/* Picker-only card, nothing to lose on either: the read failure means the
          highlighted option is the '6h' default rather than what is stored; the
          save failure means the click did not persist. */}
      {cfgError && (
        <ErrorNotice variant="inline" className="mt-1.5" message={i18nT('pages.settings.securityPanel.yolo_duration_load_failed')} askAgent />
      )}
      {save.isError && (
        <ErrorNotice variant="inline" className="mt-1.5" message={i18nT('pages.settings.securityPanel.failed_to_save_yolo_duration')} askAgent />
      )}
    </SettingsCard>
  )
}

/* ── Flagged-file delivery consent ──────────────────────────────────────────
 *
 * A VIEW over the grant the `/api/file-delivery/consent` endpoints already own.
 * It introduces no consent scope of its own and writes nothing directly: the
 * record sits on the sandbox-sealed keystone floor and the owner-gated handler
 * is its only writer, so this card can ask for exactly the two transitions the
 * endpoints already expose (record, withdraw) and earns the same refusal any
 * other caller would if it is not the owner. Routing through the endpoints
 * rather than writing settings is the point: duplicated authorization logic is
 * how two copies drift until one of them is the permissive one.
 *
 * WHY THE CACHED READ IS NOT THE AUTHORITY. An external dashboard app can reach
 * the host QueryClient and rewrite any key (#8394), so a cached "confirmed" is
 * not evidence that delivery is confirmed. Nothing is decided here — every
 * delivery gate re-reads the store server-side — and after a write this card
 * INVALIDATES rather than asserting the new state into the cache, so what it
 * shows always came from a fresh owner-gated GET rather than from something it
 * told itself.
 *
 * The rows and the excluded legs are both rendered FROM the response, never from
 * a local list, so this card cannot offer a class the backend would refuse. The
 * exclusion is STATED rather than omitted: an absent control reads as an
 * oversight, while a named one reads as the decision it is.
 */
export function FileDeliveryConsentCard() {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery<FileDeliveryConsentStatus>({
    queryKey: ['file-delivery-consent'],
    queryFn: api.fileDeliveryConsent,
  })
  // The armed request (if any): after Allow delivery is clicked, the grant is
  // NOT recorded here. The browser gets back only this nonce-free view naming
  // the host command that finishes it, so an agent-driven browser cannot
  // self-grant. Polled while armed so the "expires in" countdown and the flip to
  // confirmed both surface without a manual refresh.
  const armed = useQuery<ArmedFileDeliveryConsent>({
    queryKey: ['file-delivery-consent-arm'],
    queryFn: api.fileDeliveryConsentArmStatus,
    refetchInterval: q => (q.state.data?.armed ? 5000 : false),
  })
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['file-delivery-consent'] })
    void qc.invalidateQueries({ queryKey: ['file-delivery-consent-arm'] })
  }
  // Arming replaces the previous request, so it is safe to re-arm; on success we
  // refetch both the arm status (to show the command) and the grant (in case an
  // approve already landed).
  const arm = useMutation({
    mutationFn: (destinationClass: string) => api.armFileDeliveryConsent(destinationClass),
    onSuccess: invalidate,
  })
  const withdraw = useMutation({
    mutationFn: (destinationClass: string) => api.revokeFileDeliveryConsent(destinationClass),
    onSuccess: invalidate,
  })

  // An unreadable state renders NO state, and `isError` is the whole test for
  // that -- NOT `data === undefined`. react-query RETAINS the last good `data`
  // when a refetch rejects, so after a write that succeeded and a re-read that
  // did not, `data` still describes the state from BEFORE the write: it would
  // render "Not confirmed" for a grant that is now live. That is the reassuring
  // direction of wrong, and the dangerous one, because it tells the owner there
  // is nothing to withdraw. Everything displayed therefore comes from `view`,
  // which is empty while the read is in error, so the two failure paths (first
  // read failed, refetch failed) are identical on screen and the failure notice
  // below is the only thing that renders.
  const view = isError ? undefined : data
  const armedView = armed.isError ? undefined : armed.data

  // Copy affordance for the host command (item: parity with BrowserPanel /
  // AboutPanel, which pair a host command with a Copy button because a mistype
  // inside the step-up window silently fails approval). Acknowledge only on
  // resolution; a false "Copied" is worse than none.
  const [cmdCopied, setCmdCopied] = useState(false)
  const [cmdCopyFailed, setCmdCopyFailed] = useState(false)
  useEffect(() => {
    if (!cmdCopied) return
    const id = window.setTimeout(() => setCmdCopied(false), 1500)
    return () => window.clearTimeout(id)
  }, [cmdCopied])
  // Whether a request has been armed at least once this mount, so an armed box
  // that later disappears (expiry/consume) leaves a trace instead of a silent
  // unmount that reads as a dead button.
  const [wasArmed, setWasArmed] = useState(false)
  const anyArmed = !!armedView?.armed
  useEffect(() => {
    if (anyArmed) {
      setWasArmed(true)
      return
    }
    if (wasArmed) {
      // Host approval or expiry ends arm polling. Re-read the grant before
      // rendering the terminal state, or a completed approval looks expired.
      void qc.invalidateQueries({ queryKey: ['file-delivery-consent'] })
    }
  }, [anyArmed, qc, wasArmed])

  const excluded = view?.never_grantable ?? []
  const busy = isLoading || arm.isPending || withdraw.isPending

  return (
    <SettingsCard>
      <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.file_delivery_title')}</div>
      <div className="text-[12px] text-muted mt-0.5 mb-2 leading-relaxed">{i18nT('pages.settings.securityPanel.file_delivery_desc')}</div>

      {/* One row per GRANTABLE class, from the response. The backend returns
          exactly one today; iterating means a second class needs no change here
          and — the reason that matters — that this card can never render a
          control for a class the backend did not name. */}
      <div className="flex flex-col gap-1.5">
        {(view?.grantable ?? []).map(destinationClass => {
          const held = view?.grants?.[destinationClass] ?? null
          const isArmedForThis = !!armedView?.armed && armedView.destination_class === destinationClass
          return (
            <div
              key={destinationClass}
              data-testid={`file-delivery-row-${destinationClass}`}
              className="flex flex-col gap-1.5 border border-border rounded-md px-3 py-2"
            >
              <div className="flex items-start gap-2">
                <div className="flex-1 min-w-0">
                  <div className="text-[12px] text-text flex items-center gap-1.5 flex-wrap">
                    {/* The label is the server's, falling back to the raw id: a
                        class the backend added without a label still renders as
                        itself rather than as an empty row. */}
                    <span>{view?.labels?.[destinationClass] ?? destinationClass}</span>
                    {held
                      ? <Badge variant="ok">{i18nT('pages.settings.securityPanel.file_delivery_state_confirmed')}</Badge>
                      : isArmedForThis
                        ? <Badge variant="muted">{i18nT('pages.settings.securityPanel.file_delivery_state_armed')}</Badge>
                        : <Badge variant="muted">{i18nT('pages.settings.securityPanel.file_delivery_state_not_confirmed')}</Badge>}
                  </div>
                  {/* Plain-language destination helper at the point of consent:
                      the label alone ("this computer and your dashboard Files
                      view") still leaves a first-time reader unsure WHAT that
                      means, so name it in words they can picture. */}
                  <div className="text-[11px] text-muted mt-0.5 leading-relaxed">
                    {i18nT('pages.settings.securityPanel.file_delivery_destination_help')}
                  </div>
                  {held && (
                    <div className="text-[11px] text-muted mt-0.5">
                      {i18nT('pages.settings.securityPanel.file_delivery_since', { time: fmtDateTime(held.granted_at) })}
                    </div>
                  )}
                </div>
                {held
                  ? (
                    <Btn disabled={busy} onClick={() => withdraw.mutate(destinationClass)}>
                      {i18nT('pages.settings.securityPanel.file_delivery_withdraw')}
                    </Btn>
                  )
                  : isArmedForThis
                    ? (
                      // Armed: the next action is running the host command, not
                      // clicking again. A disabled secondary CTA labelled
                      // "Waiting..." stops the identical purple button from
                      // reading as a dead no-op on a re-click (a re-arm is
                      // invisible otherwise).
                      <Btn disabled aria-disabled="true">
                        {i18nT('pages.settings.securityPanel.file_delivery_waiting')}
                      </Btn>
                    )
                    : (
                      <Btn primary disabled={busy} onClick={() => arm.mutate(destinationClass)}>
                        {i18nT('pages.settings.securityPanel.file_delivery_confirm')}
                      </Btn>
                    )}
              </div>

              {/* Resting state, before any click: the primary button reads
                  "Allow delivery", which a blind reader takes as an immediate,
                  irreversible grant. It is neither — the click only ARMS a
                  reversible two-step request that delivers nothing until the
                  host command runs. Say so at the point of consent. */}
              {!held && !isArmedForThis && (
                <div className="text-[11px] text-muted mt-0.5 leading-relaxed">
                  {i18nT('pages.settings.securityPanel.file_delivery_confirm_help')}
                </div>
              )}

              {/* Armed but not yet approved: the grant is deliberately NOT
                  recorded by the click. Show the exact host command that
                  finishes it, because that step-up is what stops an
                  agent-driven browser from self-granting. */}
              {!held && isArmedForThis && (
                <div
                  data-testid={`file-delivery-armed-${destinationClass}`}
                  className="rounded-md border border-border bg-bg-hover px-2.5 py-2 mt-0.5"
                >
                  <div className="text-[11px] text-text leading-relaxed">
                    {i18nT('pages.settings.securityPanel.file_delivery_armed_help')}
                  </div>
                  <div className="flex items-center gap-2 mt-1 flex-wrap">
                    <code className="flex-1 min-w-0 truncate text-[11px] font-mono text-text bg-bg rounded px-2 py-1 select-all" title={armedView?.approve_command || 'kirocrew file-delivery approve'}>
                      {armedView?.approve_command || 'kirocrew file-delivery approve'}
                    </code>
                    <Btn
                      // Acknowledge only on RESOLUTION: copyToClipboard guards a
                      // missing Clipboard API and falls back to execCommand, so a
                      // plain-HTTP dashboard still copies; a false "Copied" would
                      // let the owner paste stale text into a 10-minute window.
                      onClick={() => {
                        setCmdCopyFailed(false)
                        copyToClipboard(armedView?.approve_command || 'kirocrew file-delivery approve').then(
                          ok => { if (ok) setCmdCopied(true); else setCmdCopyFailed(true) },
                          () => { setCmdCopied(false); setCmdCopyFailed(true) },
                        )
                      }}
                      aria-label={i18nT('pages.settings.securityPanel.file_delivery_copy_command')}
                    >
                      {cmdCopied ? <Check size={12} /> : <Copy size={12} />}
                      {cmdCopied
                        ? i18nT('pages.settings.securityPanel.file_delivery_copied')
                        : i18nT('pages.settings.securityPanel.file_delivery_copy')}
                    </Btn>
                  </div>
                  {cmdCopyFailed && (
                    <ErrorNotice
                      variant="inline"
                      className="mt-1.5"
                      message={i18nT('pages.settings.securityPanel.file_delivery_copy_failed')}
                      askAgent
                      onDismiss={() => setCmdCopyFailed(false)}
                    />
                  )}
                  {typeof armedView?.expires_in === 'number' && (
                    <div className="text-[11px] text-muted mt-1">
                      {i18nT('pages.settings.securityPanel.file_delivery_armed_expires', {
                        // Human duration in LONG form ("about 10 minutes", not
                        // "10 min"): the step-up deadline is safety-relevant, so
                        // it must not read as an abbreviation. Rounded to the
                        // nearest minute since the TTL is coarse.
                        duration: fmtDuration(
                          [[Math.max(1, Math.round(armedView.expires_in / 60)), 'minute']],
                          { unitDisplay: 'long' },
                        ),
                      })}
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* A request armed earlier this session has vanished (expired or
          consumed) and left no recorded grant: say so, rather than letting the
          command box silently unmount — which reads as the step-up having
          worked when it may simply have timed out. */}
      {wasArmed && !anyArmed && (view?.grantable ?? []).every(c => !(view?.grants?.[c])) && (
        <p className="text-[11px] text-muted mt-2 leading-relaxed">
          {i18nT('pages.settings.securityPanel.file_delivery_armed_expired')}
        </p>
      )}

      {/* Mirrors the tailnet card's `pinned` shape: a Lock plus one read-only
          sentence, because these legs are not merely unset — they can never be
          granted, and the upload gate they route through does not read the
          consent store at all. */}
      {excluded.length > 0 && (
        <p className="text-[12px] text-muted mt-2 leading-relaxed flex items-start gap-1">
          <Lock size={12} className="shrink-0 mt-[3px]" aria-hidden="true" />
          <span>{i18nT('pages.settings.securityPanel.file_delivery_never_grantable')}</span>
        </p>
      )}

      {/* Read and write failures are reported separately: a failed read means the
          state shown is unknown rather than unconfirmed, while a failed write
          means the click did not persist. */}
      {isError && (
        <ErrorNotice variant="inline" className="mt-1.5" message={i18nT('pages.settings.securityPanel.file_delivery_load_failed')} askAgent />
      )}
      {(arm.isError || withdraw.isError) && (
        <ErrorNotice variant="inline" className="mt-1.5" message={i18nT('pages.settings.securityPanel.file_delivery_save_failed')} askAgent />
      )}
      {/* A failed arm-status read must SAY so rather than silently dropping the
          command panel: without this, an armed request whose status GET fails
          leaves the owner with no way to see how to finish approval, and no
          explanation. Surfaced like the sibling read/write errors above. */}
      {armed.isError && (
        <ErrorNotice variant="inline" className="mt-1.5" message={i18nT('pages.settings.securityPanel.file_delivery_arm_status_failed')} askAgent />
      )}
    </SettingsCard>
  )
}
