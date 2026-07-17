import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Zap } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { useProvider } from '../../providers'

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
    onError: () => { alert('Save failed') },
  })

  return (
    <Card><CardTitle>Agent Config ({provider.labels.configFile}) <InfoTip text={`The ${provider.labels.sessionProcess} agent definition — prompt, tools, MCP servers, model settings. This is the execution layer config, separate from KiroCrew's operational bindings.`} /> <Btn onClick={() => {
      try { const config = JSON.parse(cfg); saveMut.mutate(config) } catch { alert('Invalid JSON') }
    }}>{saveMut.isSuccess ? <><Check className="lucide-inline" /> Saved</> : 'Save'}</Btn></CardTitle>
      <p className="text-muted text-[13px] mb-3">After saving, use <Zap className="lucide-inline" /> Apply & Restart Sessions at the top to apply changes.</p>
      <textarea aria-label="Agent config JSON" className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text font-mono text-[13px] outline-none resize-y leading-normal transition-colors focus-ring" rows={16} value={cfg} onChange={e => setCfg(e.target.value)} placeholder="Loading…" />
    </Card>
  )
}
