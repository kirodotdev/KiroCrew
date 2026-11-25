/**
 * Settings -> Remote Crew: how many remote Crews stay loaded, and what the
 * loaded ones cost this machine. Two rows of `RemoteCrewSettingsSection`.
 *
 * - `WarmCapSetting` writes `instances.warm_set_cap`. 0 is automatic
 *   (`WARM_SET_CAP_AUTO_CEILING`); an explicit value is 1..20, or up to a
 *   larger value already stored outside this page, which is never cut. The gateway
 *   resolves the cap on every GET /api/instances, so a save takes effect on
 *   the next read: no restart.
 * - `RemoteCrewUsageRow` asks the desktop shell for the memory and CPU of the
 *   loaded panes. It draws nothing in a browser, where there is no shell.
 */
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { Btn } from '../../components/ui'
import { SettingsStepper } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { paneOriginFor } from '../../lib/paneCache'
import { useAppSelector } from '../../store'
import { WARM_SET_CAP_AUTO_CEILING } from '../../utils/remoteCrew'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'

const CAP_KEY = 'instances.warm_set_cap'
export const WARM_CAP_MIN = 1
export const WARM_CAP_MAX = 20
/** How often the usage row re-reads the shell's numbers. */
export const USAGE_POLL_MS = 5000

interface InstancesConfig { instances?: { warm_set_cap?: unknown } }

/** The stored cap: 0 (automatic) for anything that is not a positive integer. */
function storedCap(cfg: InstancesConfig | undefined): number {
  const raw = cfg?.instances?.warm_set_cap
  return typeof raw === 'number' && Number.isFinite(raw) && raw >= 1 ? Math.round(raw) : 0
}

export function WarmCapSetting() {
  const qc = useQueryClient()
  const { data, error: readError } = useQuery<InstancesConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  // Until the stored value is read, the stepper would show a guess, so it stays off.
  const unread = data === undefined
  // The raw failure text, kept verbatim for ErrorNotice's journal match.
  const [error, setError] = useState<string | null>(null)
  const save = useMutation({
    mutationFn: (value: number) => api.patchConfig(CAP_KEY, value),
    onMutate: () => setError(null),
    // Both reads change: the config holds the stored value, GET /api/instances
    // the resolved cap the viewport trims to.
    onSuccess: () => Promise.all([
      qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
      qc.invalidateQueries({ queryKey: ['instances'] }),
    ]),
    onError: (e) => { setError(e instanceof Error ? e.message : '') },
  })
  const stored = save.isPending && typeof save.variables === 'number' ? save.variables : storedCap(data)
  const auto = stored === 0
  const shown = auto ? WARM_SET_CAP_AUTO_CEILING : stored
  // A stored value above WARM_CAP_MAX was set outside this page: the range widens to
  // it, so no step or typed value here saves less than the operator asked for.
  const max = Math.max(WARM_CAP_MAX, stored)
  const put = (n: number) => {
    // At the max, + clamps to the stored value and the equality check below
    // drops it: SettingsStepper has no increment-disabled prop.
    const next = Math.min(max, Math.max(WARM_CAP_MIN, Math.round(n)))
    if (!auto && next === stored) return
    save.mutate(next)
  }
  return (
    <div data-testid="warm-cap-setting">
      <SettingsStepper
        label={i18nT('pages.settings.remoteCrewPanel.warm_cap')}
        hint={i18nT('pages.settings.remoteCrewPanel.warm_cap_desc', { auto: WARM_SET_CAP_AUTO_CEILING })}
        description={unread ? undefined : auto
          ? i18nT('pages.settings.remoteCrewPanel.warm_cap_auto', { n: WARM_SET_CAP_AUTO_CEILING })
          : i18nT('pages.settings.remoteCrewPanel.warm_cap_set', { n: stored })}
        value={shown}
        min={WARM_CAP_MIN}
        max={max}
        onSet={put}
        onIncrement={() => put(shown + 1)}
        onDecrement={() => put(shown - 1)}
        decrementDisabled={shown <= WARM_CAP_MIN}
        disabled={save.isPending || unread}
        configKey={CAP_KEY}
      />
      {readError && (
        // No hand-off: this sits in Settings → Remote Crew, whose Add Crew
        // forms hold unsaved fields an agent chat would navigate away from.
        // The query retries on its own when the page regains focus.
        <ErrorNotice
          variant="inline"
          impact={unread ? 'blocking' : 'partial'}
          title={i18nT('pages.settings.remoteCrewPanel.warm_cap_load_failed')}
          // With cached data still shown, the notice says that value may be out of date.
          message={unread
            ? readError instanceof Error && readError.message ? readError.message : i18nT('pages.settings.remoteCrewPanel.warm_cap_load_failed')
            : i18nT('commandCenter.stale')}
          testId="warm-cap-load-error"
          className="flex-wrap pb-1.5"
        />
      )}
      {!auto && (
        <div className="flex justify-end pb-1.5">
          <Btn onClick={() => save.mutate(0)} disabled={save.isPending}>
            {i18nT('pages.settings.remoteCrewPanel.warm_cap_use_auto')}
          </Btn>
        </div>
      )}
      {error !== null && (
        // No hand-off: this sits in Settings → Remote Crew, whose Add Crew
        // forms hold unsaved fields an agent chat would navigate away from.
        // Stepping again retries in place.
        <ErrorNotice
          variant="inline"
          title={error ? i18nT('pages.settings.remoteCrewPanel.warm_cap_save_failed') : undefined}
          message={error || i18nT('pages.settings.remoteCrewPanel.warm_cap_save_failed')}
          onDismiss={() => setError(null)}
          testId="warm-cap-error"
          className="flex-wrap pb-1.5"
        />
      )}
    </div>
  )
}

export function RemoteCrewUsageRow() {
  const warm = useAppSelector(s => s.instances.warm)
  const bridge = typeof window !== 'undefined' ? window.electronAPI?.remoteCrewUsage : undefined
  const origins = useMemo(
    () => Object.values(warm).map(w => paneOriginFor(w.port)),
    [warm],
  )
  const [usage, setUsage] = useState<RemoteCrewUsage | null>(null)
  // The last read's failure text ('' when it carried none); null after a good read.
  const [readError, setReadError] = useState<string | null>(null)
  useEffect(() => {
    if (typeof bridge !== 'function') return
    let live = true
    const read = () => {
      bridge(origins).then(
        u => { if (live) { setUsage(u); setReadError(null) } },
        // A failed read drops the old numbers: they would look current.
        e => { if (live) { setUsage(null); setReadError(e instanceof Error ? e.message : '') } },
      )
    }
    read()
    const timer = setInterval(read, USAGE_POLL_MS)
    return () => { live = false; clearInterval(timer) }
  }, [bridge, origins])
  if (typeof bridge !== 'function') return null
  return (
    <div className="py-2" data-testid="remote-crew-usage">
      <span className="block text-[13px] font-semibold text-text">
        {i18nT('pages.settings.remoteCrewPanel.usage_label')}
      </span>
      {readError !== null ? (
        // No hand-off: this sits in Settings → Remote Crew, whose Add Crew
        // forms hold unsaved fields an agent chat would navigate away from.
        // The next poll retries on its own.
        <ErrorNotice
          variant="inline"
          title={readError ? i18nT('pages.settings.remoteCrewPanel.usage_failed') : undefined}
          message={readError || i18nT('pages.settings.remoteCrewPanel.usage_failed')}
          testId="remote-crew-usage-error"
          className="flex-wrap"
        />
      ) : <span className="block text-[12px] text-muted" data-testid="remote-crew-usage-value">
        {!usage ? '\u2026' : usage.panes === 0
          ? i18nT('pages.settings.remoteCrewPanel.usage_none')
          : i18nT('pages.settings.remoteCrewPanel.usage_value', {
            panes: fmtNumber(usage.panes),
            mb: fmtNumber(usage.memoryMb),
            cpu: fmtNumber(usage.cpuPercent, { maximumFractionDigits: 1 }),
          })}
      </span>}
      {usage && usage.sharedPanes > 0 && (
        <span className="block text-[12px] text-muted" data-testid="remote-crew-usage-shared">
          {i18nT('pages.settings.remoteCrewPanel.usage_shared', { n: fmtNumber(usage.sharedPanes) })}
        </span>
      )}
    </div>
  )
}
