/**
 * Settings → About → Installed app updates: whether installed apps update
 * themselves (`app_updates.mode`).
 *
 * A switch, like the gateway's own "Update the gateway automatically" row on the
 * same page. Off leaves the bell note the store already posts once per new
 * version. On (`on`) has the gateway check every 6 hours and install the version
 * once the app is idle, through the same steps as the app page's Update button;
 * a version that asks for new permissions is never installed this way and posts
 * a note instead. Each app can override this on its own page.
 *
 * The row writes through the generic config PATCH and shows the flip at once
 * through `useOptimisticConfigPaths`, the same as the per-app model rows.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { SettingsCard, SettingsToggle } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { setConfigPathValue, useOptimisticConfigPaths } from './useOptimisticConfigPaths'

// The config path, joined from its segments: a dotted literal whose first segment
// is snake_case reads to the i18n gate as copy.
export const APP_UPDATE_MODE_PATH = ['app_updates', 'mode'].join('.')

export type AppUpdateMode = 'off' | 'on'

type ConfigWithAppUpdates = { app_updates?: { mode?: unknown } }

/** The stored mode, with anything unreadable shown as the gateway reads it: off. */
export function storedAppUpdateMode(config: ConfigWithAppUpdates | undefined): AppUpdateMode {
  return config?.app_updates?.mode === 'on' ? 'on' : 'off'
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
  const shownMode = overlay.shown<string>(APP_UPDATE_MODE_PATH, storedAppUpdateMode(configQ.data))

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
        <SettingsToggle
          label={i18nT('pages.settings.appUpdates.mode_label')}
          description={i18nT('pages.settings.appUpdates.mode_hint')}
          checked={shownMode === 'on'}
          onChange={on => mode.mutate(on ? 'on' : 'off')}
          disabled={!configQ.isSuccess}
          configKey="app_updates.mode"
        />
      </SettingsCard>
    </div>
  )
}
