import { useEffect, useRef } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsToggle } from '../../components/settings'
import { Badge } from '../../components/ui'

export const ORCHESTRATOR_MEMORY_CONFIG_KEY = 'agent.orchestrator_memory'

export function OrchestratorMemoryToggle() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const config = useQuery<{ agent?: { orchestrator_memory?: boolean } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const save = useMutation({
    mutationFn: (value: boolean) => api.patchConfig(ORCHESTRATOR_MEMORY_CONFIG_KEY, value),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
  })

  return (
    <div className="space-y-2">
      <SettingsToggle
        configKey="agent.orchestrator_memory"
        label={t('pages.developer.agentBackendTab.orchestrator_memory_label')}
        labelBadge={
          <Badge variant="aim" className="text-[11px] font-sans px-1.5 py-0 flex-none">
            {t('pages.developer.agentBackendTab.orchestrator_memory_preview')}
          </Badge>
        }
        description={t('pages.developer.agentBackendTab.orchestrator_memory_description')}
        hint={t('pages.developer.agentBackendTab.orchestrator_memory_hint')}
        checked={config.data?.agent?.orchestrator_memory === true}
        disabled={!config.data || config.isError}
        busy={save.isPending}
        onChange={value => save.mutate(value)}
      />
      {save.isError && (
        <ErrorNotice
          message={t('pages.developer.agentBackendTab.orchestrator_memory_save_failed')}
          variant="inline"
          askAgent
        />
      )}
    </div>
  )
}

/** Settings search must open KAS before its memory anchor can be highlighted. */
export function OrchestratorMemoryDeepLink({ onOpen }: { onOpen: () => void }) {
  const [params] = useSearchParams()
  const highlight = params.get('highlight')
  const wanted = highlight === `key:${ORCHESTRATOR_MEMORY_CONFIG_KEY}` || highlight === 'agent.orchestrator-memory'
  const callback = useRef(onOpen)
  callback.current = onOpen
  useEffect(() => {
    if (wanted) callback.current()
  }, [wanted])
  return null
}
