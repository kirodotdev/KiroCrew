/**
 * Settings → About → App updates: whether installed apps update themselves
 * (`app_updates.mode`) and how often the gateway looks (`app_updates.check_interval_hours`).
 *
 * Off tells the user once per new version (the bell note the store already
 * posts). Ask puts an Update button on that note. Automatic (`on`) installs the version once
 * the app is idle, through the same steps as the app page's Update button; a
 * version that asks for new permissions is never installed this way and falls
 * back to an Ask note. Each app can override this on its own page.
 *
 * Both rows write through the generic config PATCH and show the pick at once
 * through `useOptimisticConfigPaths`, the same as the per-app model rows.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { SettingsCard, SettingsSelect } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { setConfigPathValue, useOptimisticConfigPaths } from './useOptimisticConfigPaths'

// Config paths, joined from their segments: a dotted literal whose first segment
// is snake_case reads to the i18n gate as copy.
export const APP_UPDATE_MODE_PATH = ['app_updates', 'mode'].join('.')
export const APP_UPDATE_INTERVAL_PATH = ['app_updates', 'check_interval_hours'].join('.')

export type AppUpdateMode = 'off' | 'ask' | 'on'
export const APP_UPDATE_MODES: AppUpdateMode[] = ['off', 'ask', 'on']
/** The interval choices, in hours. The gateway accepts 1 to 168. */
export const APP_UPDATE_INTERVALS = [1, 3, 6, 12, 24, 72, 168]

type ConfigWithAppUpdates = { app_updates?: { mode?: unknown; check_interval_hours?: unknown } }

/** The stored mode, with anything unreadable shown as the gateway reads it: off. */
export function storedAppUpdateMode(config: ConfigWithAppUpdates | undefined): AppUpdateMode {
  const raw = config?.app_updates?.mode
  return raw === 'ask' || raw === 'on' ? raw : 'off'
}

/** The stored interval in hours, defaulting to six like the gateway. */
export function storedAppUpdateInterval(config: ConfigWithAppUpdates | undefined): number {
  const raw = config?.app_updates?.check_interval_hours
  return typeof raw === 'number' && Number.isInteger(raw) && raw >= 1 && raw <= 168 ? raw : 6
}

export function appUpdateModeLabel(mode: AppUpdateMode): string {
  if (mode === 'on') return i18nT('pages.settings.appUpdates.mode_on')
  if (mode === 'ask') return i18nT('pages.settings.appUpdates.mode_ask')
  return i18nT('pages.settings.appUpdates.mode_off')
}

function intervalLabel(hours: number): string {
  // Unit abbreviations rather than counted words, so no locale needs a plural form.
  return hours % 24 === 0
    ? i18nT('pages.settings.appUpdates.interval_days', { days: hours / 24 })
    : i18nT('pages.settings.appUpdates.interval_hours', { hours })
}

export function AppUpdatesSection() {
  const qc = useQueryClient()
  const overlay = useOptimisticConfigPaths(qc)
  const [failed, setFailed] = useState<string | null>(null)
  const configQ = useQuery<ConfigWithAppUpdates>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const mode = useMutation(
    overlay.mutationOpts<string>({
      queryKey: ['kirocrewConfig'],
      mutationFn: (value: string) => api.patchConfig(APP_UPDATE_MODE_PATH, value),
      path: () => APP_UPDATE_MODE_PATH,
      displayValue: value => value,
      applyToCache: (cached, value) => setConfigPathValue(cached as ConfigWithAppUpdates, APP_UPDATE_MODE_PATH, value),
      onFailure: () => setFailed(APP_UPDATE_MODE_PATH),
      onSupersede: path => setFailed(prev => (prev === path ? null : prev)),
    }),
  )
  const interval = useMutation(
    overlay.mutationOpts<number>({
      queryKey: ['kirocrewConfig'],
      mutationFn: (value: number) => api.patchConfig(APP_UPDATE_INTERVAL_PATH, value),
      path: () => APP_UPDATE_INTERVAL_PATH,
      displayValue: value => value,
      applyToCache: (cached, value) => setConfigPathValue(cached as ConfigWithAppUpdates, APP_UPDATE_INTERVAL_PATH, value),
      onFailure: () => setFailed(APP_UPDATE_INTERVAL_PATH),
      onSupersede: path => setFailed(prev => (prev === path ? null : prev)),
    }),
  )
  const shownMode = overlay.shown<string>(APP_UPDATE_MODE_PATH, storedAppUpdateMode(configQ.data))
  const shownHours = overlay.shown<number>(APP_UPDATE_INTERVAL_PATH, storedAppUpdateInterval(configQ.data))
  const hours = APP_UPDATE_INTERVALS.includes(shownHours) ? APP_UPDATE_INTERVALS : [...APP_UPDATE_INTERVALS, shownHours].sort((a, b) => a - b)

  return (
    <div className="mt-6" data-testid="app-updates-section">
      <h4 className="text-base font-semibold text-text-strong">{i18nT('pages.settings.appUpdates.title')}</h4>
      <ErrorNotice
        variant="inline"
        askAgent
        className="mb-2"
        message={failed ? i18nT('pages.settings.appUpdates.save_failed') : configQ.isError ? i18nT('pages.settings.appUpdates.load_failed') : ''}
        testId="app-updates-error"
      />
      <SettingsCard>
        <SettingsSelect
          label={i18nT('pages.settings.appUpdates.mode_label')}
          hint={i18nT('pages.settings.appUpdates.mode_hint')}
          value={shownMode}
          options={APP_UPDATE_MODES}
          optionLabels={APP_UPDATE_MODES.map(appUpdateModeLabel)}
          onChange={value => mode.mutate(value)}
          disabled={!configQ.isSuccess}
          configKey="app_updates.mode"
        />
        <SettingsSelect
          label={i18nT('pages.settings.appUpdates.interval_label')}
          hint={i18nT('pages.settings.appUpdates.interval_hint')}
          value={String(shownHours)}
          options={hours.map(String)}
          optionLabels={hours.map(intervalLabel)}
          onChange={value => interval.mutate(Number(value))}
          disabled={!configQ.isSuccess}
          configKey="app_updates.check_interval_hours"
        />
      </SettingsCard>
    </div>
  )
}
