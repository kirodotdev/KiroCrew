import { AlertTriangle, CheckCircle2 } from 'lucide-react'

import type { AcpBackendProbe } from '../../api/client'
import { Badge } from '../ui'
import { i18nT } from '../../i18n/t'

/**
 * One harness's readiness as a badge, in the words first-run setup uses.
 *
 * Shared by the setup gate's "Use other coding agents" picker and Settings >
 * Agent Harness, so a harness reads the same in both places. The precedence is
 * the probe's: an absent binary outranks a cached absence, which outranks a
 * failed check, which outranks a sandbox verdict. `notOffered` is the one state
 * only Settings knows — a harness the build lists but never offers — and it
 * outranks everything, because nothing about installing or re-checking applies
 * to an option that is not on the table. `refused` is Settings' too: the row's
 * own launch verdict (`sandbox_refusal`), which outranks the install facts
 * because installing, restarting or re-checking cannot make that harness start.
 *
 * `sandboxOff` splits the sandbox verdict in two, and each pill names its own
 * cause because the two remedies are opposite. "Host can't sandbox" is a HOST
 * fact: nothing here can build the sandbox, and the remedy is on the machine.
 * But the gateway also lists a harness as blocked when the host CAN build one
 * and the operator turned it off (`agent.sandbox: off`) — "Sandbox turned off",
 * a SETTING, where a pill blaming the host would send the reader to remedies
 * that cannot change anything. Both stay `warn`: the colour says "not usable
 * yet", the words say why. The visible text is the badge's only accessible
 * text, so a screen reader hears the same cause. `sandboxOff` is read before
 * `blocked` because it is the more specific of the two.
 */
export function AgentStatusBadge({
  probe,
  blocked,
  sandboxOff = false,
  notOffered = false,
  refused = '',
}: {
  probe: AcpBackendProbe
  /** The host sandbox cannot confine this harness (setup gate only). */
  blocked: boolean
  /** The host sandbox works but is turned off in Kiro Crew's settings (setup gate only). */
  sandboxOff?: boolean
  /** This build never offers the harness (Settings only). */
  notOffered?: boolean
  /** Every session start refuses this harness on this host, and whose fix it is (Settings only). */
  refused?: '' | 'host' | 'setting'
}) {
  if (notOffered) {
    return <Badge variant="muted">{i18nT('pages.developer.agentBackendTab.word_not_offered')}</Badge>
  }
  if (refused) return <SandboxPill off={refused === 'setting'} />
  if (probe.installed === 'missing') {
    // The binary is there, on a release the gateway refuses to start: a fault to
    // fix, not an absence, so it gets the warning colour and its own word.
    if (probe.found_version) {
      return (
        <Badge variant="warn">
          <AlertTriangle className="lucide-inline" /> {i18nT('components.kiroPrerequisiteGate.agent_wrong_version')}
        </Badge>
      )
    }
    return <Badge variant="muted">{i18nT('components.kiroPrerequisiteGate.agent_not_installed')}</Badge>
  }
  if (probe.restart_required) {
    return <Badge variant="warn">{i18nT('components.kiroPrerequisiteGate.agent_restart_needed')}</Badge>
  }
  if (probe.installed === 'unknown') {
    return <Badge variant="muted">{i18nT('components.kiroPrerequisiteGate.agent_unverified')}</Badge>
  }
  if (sandboxOff) return <SandboxPill off />
  if (blocked) return <SandboxPill />
  return (
    <Badge variant="ok">
      <CheckCircle2 className="lucide-inline" /> {i18nT('components.kiroPrerequisiteGate.agent_installed')}
    </Badge>
  )
}

/** The sandbox verdict's pill: "Sandbox turned off" for the setting, else "Host can't sandbox". */
function SandboxPill({ off = false }: { off?: boolean }) {
  return (
    <Badge variant="warn">
      <AlertTriangle className="lucide-inline" />{' '}
      {off
        ? i18nT('components.kiroPrerequisiteGate.agent_sandbox_turned_off')
        : i18nT('components.kiroPrerequisiteGate.agent_host_cannot_sandbox')}
    </Badge>
  )
}
