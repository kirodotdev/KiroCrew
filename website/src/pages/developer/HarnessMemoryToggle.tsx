import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsToggle } from '../../components/settings'
import { SchemaRestartBadge } from '../../components/settingRef/RestartRequiredBadge'
import { i18nT } from '../../i18n/t'
import { setConfigPathValue, useOptimisticConfigPaths } from '../settings/useOptimisticConfigPaths'

/** The config field this switch writes. */
export const HARNESS_MEMORY_CONFIG_KEY = 'agent.harness_memory'

type KirocrewCfg = { agent?: { harness_memory?: boolean } }

/**
 * Settings > Agent Harness, inside the KAS row: start the KAS engine with its own
 * memory (Kiro Memory) on or off (`agent.harness_memory`).
 *
 * An ordinary config PATCH. Only a literal `true` is on, the rule the gateway
 * reads; a gateway whose config does not carry the key (an older one refuses the
 * write) shows nothing. The engine reads it when a KAS process starts, so the
 * schema's restart pill says a save is not yet applied.
 */
export function HarnessMemoryToggle() {
  const qc = useQueryClient()
  const cfgQ = useQuery<KirocrewCfg>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig() as Promise<KirocrewCfg>,
  })
  const overlay = useOptimisticConfigPaths(qc)
  const [saveError, setSaveError] = useState<string | null>(null)
  const toggleMut = useMutation(overlay.mutationOpts<boolean>({
    queryKey: ['kirocrewConfig'],
    mutationFn: (value: boolean) => api.patchConfig(HARNESS_MEMORY_CONFIG_KEY, value),
    path: () => HARNESS_MEMORY_CONFIG_KEY,
    displayValue: v => v,
    applyToCache: (cached, value) => setConfigPathValue(cached as KirocrewCfg, HARNESS_MEMORY_CONFIG_KEY, value),
    onFailure: () => setSaveError(i18nT('pages.developer.harnessMemory.save_failed')),
    onSupersede: () => setSaveError(null),
  }))

  const stored = cfgQ.data?.agent?.harness_memory
  if (typeof stored !== 'boolean') return null
  const shown = overlay.shown(HARNESS_MEMORY_CONFIG_KEY, stored === true)

  return (
    <div className="space-y-1" data-testid="harness-memory-toggle">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <SettingsToggle
            configKey={HARNESS_MEMORY_CONFIG_KEY}
            label={i18nT('pages.developer.harnessMemory.label')}
            description={i18nT('pages.developer.harnessMemory.description')}
            checked={shown}
            onChange={value => toggleMut.mutate(value)}
            disabled={toggleMut.isPending}
          />
        </div>
        <SchemaRestartBadge configKey={HARNESS_MEMORY_CONFIG_KEY} />
      </div>
      <ErrorNotice message={saveError} variant="inline" askAgent />
    </div>
  )
}
