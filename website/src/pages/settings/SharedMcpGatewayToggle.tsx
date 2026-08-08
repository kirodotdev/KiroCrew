import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Check, AlertTriangle } from 'lucide-react'
import Modal from '../../components/Modal'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { api } from '../../api/client'

import { i18nT } from '../../i18n/t'
type GatewayStatus = { enabled: boolean; running: boolean; ping_ok: boolean; supported: boolean }

type Phase = 'idle' | 'confirm' | 'applying' | 'done' | 'failed'

export function SharedMcpGatewayToggle() {
  const qc = useQueryClient()
  const statusQ = useQuery<GatewayStatus>({ queryKey: ['mcpGatewayStatus'], queryFn: () => api.mcpGatewayStatus() })
  const enabled = statusQ.data?.enabled ?? false
  const pingOk = statusQ.data?.ping_ok ?? false
  // Default true so a still-loading status (or an older backend that predates
  // the field) never disables the control; only a definite `false` gates it.
  const supported = statusQ.data?.supported ?? true

  const [phase, setPhase] = useState<Phase>('idle')
  const [target, setTarget] = useState(false)
  const busy = phase === 'applying'

  // In-process apply: the POST starts/stops the broker, drops + relinks all
  // agent sessions, and verifies connectivity — no gateway restart, so this
  // dashboard session stays logged in.  The response is the verified state.
  //
  // Stays on this page on success. It used to navigate to Developer > System,
  // which was wrong twice over: enabling the pool is the FIRST half of the job
  // (the user then picks which servers to pool, on this very page), and the
  // destination did not even carry the `plane` the metrics card lives on, so it
  // landed on the Sessions table instead. Reporting the verified state here and
  // letting the user choose where to go next is the honest shape.
  const run = async (next: boolean) => {
    setTarget(next)
    setPhase('applying')
    try {
      const r = await api.mcpGatewayEnable(next)
      const ok = next ? r.ping_ok : !r.running
      if (ok) qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
      setPhase(ok ? 'done' : 'failed')
    } catch {
      setPhase('failed')
    }
  }

  const subStatus = !supported ? i18nT('pages.settings.sharedMcpGatewayToggle.not_available_on_windows')
    : !enabled ? i18nT('pages.settings.sharedMcpGatewayToggle.disabled_each_session_spawns_its_own_mcp_backend')
    : pingOk ? i18nT('pages.settings.sharedMcpGatewayToggle.active_sessions_share_pooled_mcp_backends_see_th')
    : i18nT('pages.settings.sharedMcpGatewayToggle.enabled_broker_not_reachable_toggle_off_and_on_t')

  const btn = 'text-[13px] px-3 py-1.5 rounded-md transition-colors cursor-pointer'

  return (
    <SettingsSection title={i18nT('pages.settings.sharedMcpGatewayToggle.shared_mcp_gateway')}>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.sharedMcpGatewayToggle.shared_mcp_gateway')}
          description={subStatus}
          checked={enabled}
          disabled={statusQ.isLoading || busy || (!supported && !enabled)}
          onChange={next => { if (!supported && next) return; setTarget(next); setPhase('confirm') }}
        />
      </SettingsCard>

      {/* Confirm */}
      <Modal
        open={phase === 'confirm'}
        onClose={() => setPhase('idle')}
        title={target ? i18nT('pages.settings.sharedMcpGatewayToggle.enable_shared_mcp_gateway') : i18nT('pages.settings.sharedMcpGatewayToggle.disable_shared_mcp_gateway')}
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
        title={phase === 'done' ? i18nT('pages.settings.sharedMcpGatewayToggle.done') : phase === 'failed' ? i18nT('pages.settings.sharedMcpGatewayToggle.could_not_apply') : (target ? i18nT('pages.settings.sharedMcpGatewayToggle.enabling_shared_mcp_gateway') : i18nT('pages.settings.sharedMcpGatewayToggle.disabling_shared_mcp_gateway'))}
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
            {target ? i18nT('pages.settings.sharedMcpGatewayToggle.shared_mcp_gateway_is_active') : i18nT('pages.settings.sharedMcpGatewayToggle.shared_mcp_gateway_is_disabled')}
          </div>
        ) : phase === 'failed' ? (
          <div className="flex items-start gap-2 text-[13px] text-text">
            <AlertTriangle size={16} className="text-danger mt-0.5 shrink-0" />
            <span>{target
              ? i18nT('pages.settings.sharedMcpGatewayToggle.gateway_stuck_roll_back')
              : i18nT('pages.settings.sharedMcpGatewayToggle.gateway_stuck_retry')}</span>
          </div>
        ) : (
          <div className="flex items-center gap-2 text-[13px] text-text">
            <Loader2 size={16} className="text-accent animate-spin shrink-0" />
            {target ? i18nT('pages.settings.sharedMcpGatewayToggle.starting_broker_restarting_sessions_verifying_co') : i18nT('pages.settings.sharedMcpGatewayToggle.stopping_broker_and_restarting_sessions')}
          </div>
        )}
      </Modal>
    </SettingsSection>
  )
}
