import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Check } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import ErrorNotice from '../../components/ErrorNotice'
import StatusNotice from '../../components/StatusNotice'
import { useProvider } from '../../providers'

import { i18nT } from '../../i18n/t'
export default function AgentCfgTab() {
  const provider = useProvider()
  const queryClient = useQueryClient()
  const { data: loadedCfg = '' } = useQuery({
    queryKey: ['agent-config'],
    queryFn: () => api.agentConfig().then(d => JSON.stringify(d, null, 2)),
  })
  const [cfg, setCfg] = useState('')
  const [saveError, setSaveError] = useState('')
  const [validationNotice, setValidationNotice] = useState('')
  useEffect(() => { if (loadedCfg && !cfg) setCfg(loadedCfg) }, [loadedCfg, cfg])

  const saveMut = useMutation({
    mutationFn: (config: object) => api.saveAgentConfig(config),
    onMutate: () => { setSaveError(''); setValidationNotice('') },
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['agent-config'] }) },
    onError: (error: unknown) => {
      setSaveError(error instanceof Error && error.message
        ? error.message
        : i18nT('pages.overview.agentCfgTab.save_failed'))
    },
  })

  return (
    <Card><CardTitle>{i18nT('pages.overview.agentCfgTab.agent_config_file', { file: provider.labels.configFile })} <InfoTip text={i18nT('pages.overview.agentCfgTab.agent_config_tip', { process: provider.labels.sessionProcess })} /> <Btn onClick={() => {
      try { const config = JSON.parse(cfg); saveMut.mutate(config) } catch { setValidationNotice(i18nT('pages.overview.agentCfgTab.invalid_json')) }
    }}>{saveMut.isSuccess ? <><Check className="lucide-inline" /> {i18nT('pages.overview.agentCfgTab.saved')}</> : i18nT('pages.overview.agentCfgTab.save')}</Btn></CardTitle>
      <StatusNotice message={validationNotice} onDismiss={() => setValidationNotice('')} className="mb-2" testId="agent-config-validation-notice" />
      {/* No hand-off: the editor below holds unsaved config JSON. */}
      <ErrorNotice message={saveError} onDismiss={() => setSaveError('')} className="mb-2" testId="agent-config-save-error" />
      <textarea aria-label={i18nT('pages.overview.agentCfgTab.agent_config_json')} className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text font-mono text-[13px] outline-hidden resize-y leading-normal transition-colors focus-ring" rows={16} value={cfg} onChange={e => setCfg(e.target.value)} placeholder={i18nT('pages.overview.agentCfgTab.loading')} />
    </Card>
  )
}
