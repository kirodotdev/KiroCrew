/**
 * Settings -> Security -> Auto-approve: the ad-hoc auto-approve duration, the
 * owner grant that relaxes a default for a bounded time. One of the sections
 * `SecurityPanel.tsx` composes.
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Lock, CheckCircle2, Circle, Clock } from 'lucide-react'
import { useAppDispatch, useAppSelector } from '../../store'
import { setYoloDuration } from '../../store/dashboardSlice'
import { SettingsCard } from '../../components/settings'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtTimeNumeric } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'

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
