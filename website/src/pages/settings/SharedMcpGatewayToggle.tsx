import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Check, AlertTriangle } from 'lucide-react'
import Modal from '../../components/Modal'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { api } from '../../api/client'

import { i18nT } from '../../i18n/t'
type GatewayStatus = { enabled: boolean; apps_enabled: boolean; running: boolean; ping_ok: boolean; supported: boolean }

type Phase = 'idle' | 'confirm' | 'applying' | 'done' | 'failed'

/** Presentation state of the MCP Apps render switch.
 *
 * Pure so the rule is testable without rendering.
 *
 * Settable even while the broker is off, which is the point: this is an OPT-OUT
 * of executing server-authored UI, and `apps_enabled` defaults on, so gating it
 * behind a running broker would force a cautious user to enable the broker first
 * — exposing themselves to the capability — and then race to switch it off. The
 * endpoint writes config only and needs no broker, so recording the preference
 * early is both possible and the safer order. `needsGateway` drives a separate
 * line explaining that the stored choice is inert until the broker runs.
 *
 * There is no per-state description: the label describes what the switch
 * CONTROLS, not what is currently happening. A present-tense "renders in chat"
 * is false whenever the broker is off, and this control exists precisely to
 * answer "is this rendering?" — so it must not be the thing that misreports it.
 */
export function mcpAppsSwitchState(s: {
  gatewayEnabled: boolean
  appsEnabled: boolean
  loading: boolean
  busy: boolean
}): { checked: boolean; disabled: boolean; needsGateway: boolean } {
  return {
    checked: s.appsEnabled,
    disabled: s.loading || s.busy,
    needsGateway: !s.gatewayEnabled,
  }
}

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

  // Optimistic value held only for the duration of the request. The status query
  // is the source of truth, so the override is DROPPED once the refetch lands —
  // holding it indefinitely would pin this tab to its own last write and hide a
  // change made anywhere else.
  const [appsPending, setAppsPending] = useState<boolean | null>(null)
  const [appsBusy, setAppsBusy] = useState(false)
  const [appsError, setAppsError] = useState<string | null>(null)
  const [appsApplied, setAppsApplied] = useState(false)
  const appsEnabled = appsPending ?? statusQ.data?.apps_enabled ?? true
  const appsState = mcpAppsSwitchState({
    gatewayEnabled: enabled,
    appsEnabled,
    loading: statusQ.isLoading,
    busy: appsBusy,
  })

  const runApps = async (next: boolean) => {
    setAppsBusy(true)
    setAppsError(null)
    setAppsApplied(false)
    setAppsPending(next)
    try {
      const r = await api.mcpGatewayAppsEnable(next)
      // Seed the cache from the RESPONSE before invalidating. Dropping the local
      // override on the way out is only safe if the cache already carries the new
      // value — otherwise a refetch that fails leaves the switch showing the
      // stale cached state while config on disk says otherwise.
      qc.setQueryData(['mcpGatewayStatus'], (prev: GatewayStatus | undefined) =>
        prev ? { ...prev, apps_enabled: r.enabled } : prev)
      await qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
      setAppsApplied(true)
    } catch (e) {
      // Prefer the server's message: the refusals this endpoint can return are
      // actionable ("…is set in config.local.json; edit that file instead") and
      // collapsing them into one generic line throws away the only instruction
      // that would let the user fix it. `ApiError.message` carries the response
      // body's `error` prose. Generic text is the fallback, not the default.
      const msg = e instanceof Error ? e.message.trim() : ''
      setAppsError(msg || i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_failed'))
    } finally {
      setAppsPending(null)
      setAppsBusy(false)
    }
  }

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

      {/* Render switch for server-authored UI. Applies instantly with no confirm
          step: the broker re-reads this flag per tool result, so nothing restarts.
          Gated on the broker because the render and callback paths live inside it
          — shown rather than hidden while off, so its stored state stays legible. */}
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps')}
          description={i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_capability')}
          checked={appsState.checked}
          disabled={appsState.disabled}
          onChange={next => void runApps(next)}
        />
        {/* Rendered OUTSIDE SettingsToggle: as its description it would inherit a
            disabled row's opacity-40, dimming the line that explains the state. */}
        {appsState.needsGateway && (
          <div className="text-[12px] text-text-muted">
            {i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_needs_gateway')}
          </div>
        )}
        {appsApplied && !appsError && (
          <div className="text-[12px] text-text-muted">
            {i18nT('pages.settings.sharedMcpGatewayToggle.mcp_apps_applies_to_new')}
          </div>
        )}
        {appsError && (
          <div className="flex items-start gap-1.5 text-[12px] text-danger">
            <AlertTriangle size={13} className="mt-0.5 shrink-0" />
            <span>{appsError}</span>
          </div>
        )}
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
