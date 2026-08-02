import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Zap } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
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
  useEffect(() => { if (loadedCfg && !cfg) setCfg(loadedCfg) }, [loadedCfg, cfg])

  const saveMut = useMutation({
    mutationFn: (config: object) => api.saveAgentConfig(config),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['agent-config'] }) },
    onError: () => { alert(i18nT('pages.overview.agentCfgTab.save_failed')) },
  })

  return (
    <Card><CardTitle>{i18nT('pages.overview.agentCfgTab.agent_config_file', { file: provider.labels.configFile })} <InfoTip text={i18nT('pages.overview.agentCfgTab.agent_config_tip', { process: provider.labels.sessionProcess })} /> <Btn onClick={() => {
      try { const config = JSON.parse(cfg); saveMut.mutate(config) } catch { alert(i18nT('pages.overview.agentCfgTab.invalid_json')) }
    }}>{saveMut.isSuccess ? <><Check className="lucide-inline" /> {i18nT('pages.overview.agentCfgTab.saved')}</> : 'Save'}</Btn></CardTitle>
      <p className="text-muted text-[13px] mb-3">{i18nT('pages.overview.agentCfgTab.after_saving_use')} <Zap className="lucide-inline" /> {i18nT('pages.overview.agentCfgTab.apply_restart_sessions_at_the_top_to_apply_chang')}</p>
      <textarea aria-label={i18nT('pages.overview.agentCfgTab.agent_config_json')} className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text font-mono text-[13px] outline-none resize-y leading-normal transition-colors focus-ring" rows={16} value={cfg} onChange={e => setCfg(e.target.value)} placeholder={i18nT('pages.overview.agentCfgTab.loading')} />
    </Card>
  )
}
