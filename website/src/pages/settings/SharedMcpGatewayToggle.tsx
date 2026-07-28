import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Loader2, Check, AlertTriangle } from 'lucide-react'
import Modal from '../../components/Modal'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { api } from '../../api/client'

import { i18nT } from '../../i18n/t'
type GatewayStatus = { enabled: boolean; running: boolean; ping_ok: boolean }

type Phase = 'idle' | 'confirm' | 'applying' | 'done' | 'failed'

export function SharedMcpGatewayToggle() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const statusQ = useQuery<GatewayStatus>({ queryKey: ['mcpGatewayStatus'], queryFn: () => api.mcpGatewayStatus() })
  const enabled = statusQ.data?.enabled ?? false
  const pingOk = statusQ.data?.ping_ok ?? false

  const [phase, setPhase] = useState<Phase>('idle')
  const [target, setTarget] = useState(false)
  const busy = phase === 'applying'

  // In-process apply: the POST starts/stops the broker, drops + relinks all
  // agent sessions, and verifies connectivity — no gateway restart, so this
  // dashboard session stays logged in.  The response is the verified state.
  const run = async (next: boolean) => {
    setTarget(next)
    setPhase('applying')
    try {
      const r = await api.mcpGatewayEnable(next)
      const ok = next ? r.ping_ok : !r.running
      if (ok) qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
      // Land on the live gateway metrics — the McpGatewayCard renders on
      // Developer > System (P5 moved it there), not Settings Overview.
      if (ok && next) { setPhase('idle'); navigate('/developer?tab=system'); return }
      setPhase(ok ? 'done' : 'failed')
    } catch {
      setPhase('failed')
    }
  }

  const subStatus = !enabled ? 'Disabled — each session spawns its own MCP backends.'
    : pingOk ? 'Active — sessions share pooled MCP backends. See the live pool under Developer > System.'
    : 'Enabled — broker not reachable; toggle off and on to re-apply.'

  const btn = 'text-[13px] px-3 py-1.5 rounded-md transition-colors cursor-pointer'

  return (
    <SettingsSection title={i18nT('pages.settings.sharedMcpGatewayToggle.shared_mcp_gateway')}>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.sharedMcpGatewayToggle.shared_mcp_gateway')}
          description={subStatus}
          checked={enabled}
          disabled={statusQ.isLoading || busy}
          onChange={next => { setTarget(next); setPhase('confirm') }}
        />
      </SettingsCard>

      {/* Confirm */}
      <Modal
        open={phase === 'confirm'}
        onClose={() => setPhase('idle')}
        title={target ? 'Enable shared MCP gateway?' : 'Disable shared MCP gateway?'}
        maxWidth={460}
        footer={<>
          <button className={`${btn} border border-border text-text hover:bg-bg-hover`} onClick={() => setPhase('idle')}>{i18nT('pages.settings.sharedMcpGatewayToggle.cancel')}</button>
          <button className={`${btn} bg-accent text-accent-fg hover:bg-accent-hover`} onClick={() => run(target)}>{i18nT('pages.settings.sharedMcpGatewayToggle.continue')}</button>
        </>}
      >
        <div className="text-[13px] text-text">{i18nT('pages.settings.sharedMcpGatewayToggle.this_restarts_all_active_sessions_onto_the_new_m')}</div>
      </Modal>

      {/* Applying + terminal states */}
      <Modal
        open={busy || phase === 'done' || phase === 'failed'}
        onClose={() => { if (!busy) setPhase('idle') }}
        title={phase === 'done' ? 'Done' : phase === 'failed' ? 'Could not apply' : (target ? 'Enabling shared MCP gateway' : 'Disabling shared MCP gateway')}
        maxWidth={460}
        footer={phase === 'done' ? (
          <button className={`${btn} bg-accent text-accent-fg hover:bg-accent-hover`} onClick={() => setPhase('idle')}>{i18nT('pages.settings.sharedMcpGatewayToggle.close')}</button>
        ) : phase === 'failed' ? (<>
          <button className={`${btn} border border-border text-text hover:bg-bg-hover`} onClick={() => setPhase('idle')}>{i18nT('pages.settings.sharedMcpGatewayToggle.close')}</button>
          {target && <button className={`${btn} bg-danger text-white hover:opacity-90`} onClick={() => run(false)}>{i18nT('pages.settings.sharedMcpGatewayToggle.roll_back_disable')}</button>}
        </>) : undefined}
      >
        {phase === 'done' ? (
          <div className="flex items-center gap-2 text-[13px] text-text">
            <Check size={16} className="text-ok" />
            {target ? 'Shared MCP gateway is active.' : 'Shared MCP gateway is disabled.'}
          </div>
        ) : phase === 'failed' ? (
          <div className="flex items-start gap-2 text-[13px] text-text">
            <AlertTriangle size={16} className="text-danger mt-0.5 shrink-0" />
            <span>{i18nT('pages.settings.sharedMcpGatewayToggle.the_gateway_did_not_reach_the_expected_state')}{target ? ' Roll back to the safe (disabled) state, or try again.' : ' Try again.'}</span>
          </div>
        ) : (
          <div className="flex items-center gap-2 text-[13px] text-text">
            <Loader2 size={16} className="text-accent animate-spin shrink-0" />
            {target ? 'Starting broker, restarting sessions, verifying connectivity…' : 'Stopping broker and restarting sessions…'}
          </div>
        )}
      </Modal>
    </SettingsSection>
  )
}
