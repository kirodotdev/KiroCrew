/**
 * The app page's "Update automatically" row: `apps.<name>.auto_update`.
 *
 * Follow (the empty value) uses Settings → About → App updates, Always
 * installs new versions of this app whatever that setting says, and Never
 * keeps it at its installed version. Never still lets the store say a newer
 * version exists; it only stops the install.
 *
 * Shown only where the gateway could update the app on its own: an installed,
 * gateway-lifecycle app that came from a registry or the catalog.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { SettingsSelect } from '../settings'
import ErrorNotice from '../ErrorNotice'
import { i18nT } from '../../i18n/t'
import { setConfigPathValue, useOptimisticConfigPaths } from '../../pages/settings/useOptimisticConfigPaths'
import { appUpdateModeLabel, storedAppUpdateMode } from '../../pages/settings/AppUpdatesSection'
import { Card, CardTitle } from '../ui'

export type AppAutoUpdateOverride = '' | 'always' | 'never'
const OVERRIDES: AppAutoUpdateOverride[] = ['', 'always', 'never']

type ConfigWithApps = {
  apps?: Record<string, { auto_update?: unknown } | undefined>
  app_updates?: { mode?: unknown }
}

export function appAutoUpdatePath(name: string): string {
  return ['apps', name, 'auto_update'].join('.')
}

export function storedAppAutoUpdate(config: ConfigWithApps | undefined, name: string): AppAutoUpdateOverride {
  const raw = config?.apps?.[name]?.auto_update
  return raw === 'always' || raw === 'never' ? raw : ''
}

export default function AppAutoUpdateCard({ name }: { name: string }) {
  const qc = useQueryClient()
  const overlay = useOptimisticConfigPaths(qc)
  const [failed, setFailed] = useState(false)
  const configQ = useQuery<ConfigWithApps>({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const path = appAutoUpdatePath(name)
  const mut = useMutation(
    overlay.mutationOpts<string>({
      queryKey: ['kirocrewConfig'],
      mutationFn: (value: string) => api.patchConfig(path, value),
      path: () => path,
      displayValue: value => value,
      applyToCache: (cached, value) => setConfigPathValue(cached as ConfigWithApps, path, value),
      onFailure: () => setFailed(true),
      onSupersede: () => setFailed(false),
    }),
  )
  const shown = overlay.shown<string>(path, storedAppAutoUpdate(configQ.data, name))
  const globalLabel = appUpdateModeLabel(storedAppUpdateMode(configQ.data))
  const labels = OVERRIDES.map(value =>
    value === 'always'
      ? i18nT('pages.appDetailPage.auto_update_always')
      : value === 'never'
        ? i18nT('pages.appDetailPage.auto_update_never')
        : i18nT('pages.appDetailPage.auto_update_follow', { mode: globalLabel }),
  )
  return (
    <Card data-testid="app-auto-update-card">
      <CardTitle>{i18nT('pages.appDetailPage.auto_update_title')}</CardTitle>
      <ErrorNotice
        variant="inline"
        askAgent
        className="mt-1"
        message={
          failed
            ? i18nT('pages.appDetailPage.auto_update_save_failed')
            : configQ.isError
              ? i18nT('pages.settings.appUpdates.load_failed')
              : ''
        }
        testId="app-auto-update-error"
      />
      <SettingsSelect
        label={i18nT('pages.appDetailPage.auto_update_label')}
        hint={i18nT('pages.appDetailPage.auto_update_hint')}
        value={shown}
        options={OVERRIDES}
        optionLabels={labels}
        onChange={value => mut.mutate(value)}
        disabled={!configQ.isSuccess}
      />
    </Card>
  )
}
