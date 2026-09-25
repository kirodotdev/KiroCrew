import { ArrowRight, RefreshCw } from 'lucide-react'

import type { AcpBackendProbe } from '../../api/client'
import { Btn, SendBtn } from '../ui'
import { CopyCommand } from './CopyCommand'
import { i18nT } from '../../i18n/t'

/**
 * The detail of a harness whose components are absent from the gateway host:
 * what to install, the command that installs it, and what to press afterwards.
 *
 * The missing components are named only when there is no command to run:
 * beside `npm install -g @zed-industries/claude-code-acp` a "Missing:
 * claude-acp" line names a second thing, and the reader second-guesses which
 * one to install. Kiro CLI is the case with components and no one-liner — it is
 * installed from its own docs, so the probe sends `""` rather than an invented
 * command.
 */
export function AgentInstallDetail({ name, probe }: { name: string; probe: AcpBackendProbe }) {
  return (
    <>
      <p className="text-[13px] font-medium text-text">
        {i18nT('components.kiroPrerequisiteGate.agent_install_on_host', { name })}
      </p>
      {probe.install_command ? (
        <CopyCommand>
          <code>{probe.install_command}</code>
        </CopyCommand>
      ) : probe.missing_components.length > 0 ? (
        <p className="text-[12px] text-muted">
          {i18nT('components.kiroPrerequisiteGate.agent_missing_components', {
            components: probe.missing_components.join(', '),
          })}
        </p>
      ) : null}
      <p className="text-[12px] leading-relaxed text-muted">
        {i18nT('components.kiroPrerequisiteGate.agent_install_then_check', { name })}
      </p>
    </>
  )
}

/**
 * The detail's action row: the ONE control that switches the agent, and — only
 * where a re-check can change the verdict — Check again beside it.
 *
 * `onUse` absent means no Use button at all rather than a dead one: Settings
 * omits it for the harness already in use. `onRecheck` absent means no Check
 * again: there is nothing to re-check on an agent that is installed, ready and
 * allowed to start, and nothing this machine holds decides an option the build
 * never offers.
 */
export function AgentDetailActions({
  name,
  onUse,
  useDisabled = false,
  useDescribedBy,
  busy,
  switching,
  onRecheck,
  rechecking,
}: {
  name: string
  onUse?: () => void
  /** The harness cannot be used right now (absent, cached absent, …). */
  useDisabled?: boolean
  /** Id of the element stating WHY Use is dead, for `aria-describedby`. */
  useDescribedBy?: string
  /** A switch or re-check is in flight anywhere on the picker: both buttons wait. */
  busy: boolean
  /** The switch is in flight for THIS harness: the label says so. */
  switching: boolean
  onRecheck?: () => void
  rechecking: boolean
}) {
  // Nothing to press: no row rather than an empty one holding its padding.
  if (!onUse && !onRecheck) return null
  return (
    <div className="flex flex-wrap items-center gap-2 pt-1">
      {onUse && (
        <SendBtn
          type="button"
          className="inline-flex items-center gap-1.5"
          disabled={busy || useDisabled}
          aria-describedby={useDescribedBy}
          onClick={onUse}
        >
          {switching
            ? i18nT('components.kiroPrerequisiteGate.switching_agent')
            : i18nT('components.kiroPrerequisiteGate.use_agent', { name })}
          <ArrowRight className="lucide-inline" />
        </SendBtn>
      )}
      {onRecheck && (
        <Btn
          type="button"
          className="h-9 rounded-lg px-3"
          disabled={busy || rechecking}
          onClick={onRecheck}
        >
          <RefreshCw className={`lucide-inline ${rechecking ? 'animate-spin' : ''}`} />
          {i18nT('components.kiroPrerequisiteGate.check_again')}
        </Btn>
      )}
    </div>
  )
}
