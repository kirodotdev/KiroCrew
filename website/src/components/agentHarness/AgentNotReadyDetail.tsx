import type { AcpBackendProbe } from '../../api/client'
import { AgentInstallDetail } from './AgentDetail'
import { CopyCommand } from './CopyCommand'
import { i18nT } from '../../i18n/t'

/**
 * Why a harness cannot start a session yet, one value per cause a setup screen
 * knows how to explain. The screen picks the value; this module only words it,
 * so the picker row and the configured-agent notice say the same thing for the
 * same cause.
 */
export type AgentNotReadyReason =
  | 'not_offered'
  | 'missing'
  | 'restart_required'
  | 'unverified'
  | 'sandbox_off'
  | 'sandbox_blocked'
  | 'agent_specs_missing'
  | 'agent_specs_rejected'

/**
 * The explanation for one {@link AgentNotReadyReason}, with the command that
 * fixes it where there is one. Commands are `<code>` children, never catalog
 * values: a translated command cannot be typed.
 */
export function AgentNotReadyDetail({
  reason,
  name,
  probe,
}: {
  reason: AgentNotReadyReason
  name: string
  probe: AcpBackendProbe
}) {
  switch (reason) {
    case 'not_offered':
      return (
        <p className="text-[13px] leading-relaxed text-text">
          {i18nT('pages.developer.agentBackendTab.not_offered_by_this_build')}
        </p>
      )
    case 'missing':
      return <AgentInstallDetail name={name} probe={probe} />
    case 'restart_required':
      return (
        <p className="text-[13px] leading-relaxed text-text">
          {i18nT('components.kiroPrerequisiteGate.agent_restart_required_detail', { name })}
        </p>
      )
    case 'unverified':
      return (
        <p className="text-[13px] leading-relaxed text-text">
          {i18nT('components.kiroPrerequisiteGate.agent_unverified_detail', { name })}
        </p>
      )
    case 'sandbox_off':
      // Same blocked verdict as 'sandbox_blocked', different cause: no doctor
      // command, because nothing it measures can turn a setting back on.
      return (
        <p className="text-[13px] leading-relaxed text-text" role="status">
          {i18nT('components.kiroPrerequisiteGate.sandbox_off_agent_detail', { name })}
        </p>
      )
    case 'sandbox_blocked':
      return (
        <div className="space-y-2 text-[13px] leading-relaxed text-text" role="status">
          <p>
            {i18nT('components.kiroPrerequisiteGate.sandbox_blocked_agent_detail', { name })}
          </p>
          <p className="text-muted">
            {i18nT('components.kiroPrerequisiteGate.run_kirocrew_doctor_on_the_gateway_host_for_a_ful')}
          </p>
          <CopyCommand><code>kirocrew doctor</code></CopyCommand>
        </div>
      )
    case 'agent_specs_missing':
      // The installer's own spec step: it writes the specs on a first run too,
      // where no repair card is shown yet.
      return (
        <div className="space-y-2 text-[13px] leading-relaxed text-text" role="status">
          <p>{i18nT('components.kiroPrerequisiteGate.configured_agent_specs_missing', { name })}</p>
          <CopyCommand><code>kirocrew setup --agent-only</code></CopyCommand>
        </div>
      )
    case 'agent_specs_rejected':
      return (
        <div className="space-y-2 text-[13px] leading-relaxed text-text" role="status">
          <p>{i18nT('components.kiroPrerequisiteGate.configured_agent_specs_rejected', { name })}</p>
          <p className="text-muted">
            {i18nT('components.kiroPrerequisiteGate.remedy_spec_rejected_rewrite')}
          </p>
          <CopyCommand><code>kirocrew setup --agent-only --clean</code></CopyCommand>
        </div>
      )
  }
}
