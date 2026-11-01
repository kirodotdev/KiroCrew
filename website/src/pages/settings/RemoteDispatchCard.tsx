import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsToggle } from '../../components/settings'
import { i18nT } from '../../i18n/t'
import { setConfigPathValue, useOptimisticConfigPaths } from './useOptimisticConfigPaths'

/** The config path this card's switch writes, joined as `previewFlagConfigPath` does. */
const configPath = ['work_ledger', 'remote_dispatch'].join('.')

type KirocrewCfg = { work_ledger?: { remote_dispatch?: boolean } }

/**
 * Settings > Developer > Feature Previews: conductor remote dispatch
 * (`docs/request-for-change/rfc-conductor-remote-dispatch.md`).
 *
 * Like the KAS card, the switch is not a `dashboard.feature_previews` flag: it is
 * the gateway's own `work_ledger.remote_dispatch`, which every remote-dispatch path
 * reads per call, so the card writes it through the ordinary config PATCH and a
 * save applies with no restart. Only a literal `true` is on, the rule the gateway
 * reads. Hidden while the config read has not answered or the gateway's config
 * does not carry the key (an older gateway refuses the write); a failed read says so.
 */
export function RemoteDispatchCard() {
  const qc = useQueryClient()
  const cfgQ = useQuery<KirocrewCfg>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig() as Promise<KirocrewCfg>,
  })
  const overlay = useOptimisticConfigPaths(qc)
  const [saveError, setSaveError] = useState<string | null>(null)
  const toggleMut = useMutation(overlay.mutationOpts<boolean>({
    queryKey: ['kirocrewConfig'],
    mutationFn: (value: boolean) => api.patchConfig(configPath, value),
    path: () => configPath,
    displayValue: v => v,
    applyToCache: (cached, value) => setConfigPathValue(cached as KirocrewCfg, configPath, value),
    onFailure: () => setSaveError(i18nT('pages.developer.featurePreviewsTab.remote_dispatch_save_failed')),
    onSupersede: () => setSaveError(null),
  }))

  if (cfgQ.isError) {
    // A failed read must not look like a gateway without the switch.
    return (
      <SettingsCard>
        <ErrorNotice
          message={i18nT('pages.developer.featurePreviewsTab.remote_dispatch_load_failed')}
          variant="inline"
          askAgent
          testId="remote-dispatch-load-error"
        />
      </SettingsCard>
    )
  }
  const stored = cfgQ.data?.work_ledger?.remote_dispatch
  if (typeof stored !== 'boolean') return null
  const shown = overlay.shown(configPath, stored === true)

  return (
    <SettingsCard>
      <div className="space-y-2" data-testid="remote-dispatch-card">
        <SettingsToggle
          configKey="work_ledger.remote_dispatch"
          label={i18nT('pages.developer.featurePreviewsTab.remote_dispatch')}
          description={i18nT('pages.developer.featurePreviewsTab.remote_dispatch_desc')}
          checked={shown}
          onChange={value => toggleMut.mutate(value)}
          disabled={toggleMut.isPending}
        />
        <ErrorNotice message={saveError} variant="inline" askAgent />
      </div>
    </SettingsCard>
  )
}
