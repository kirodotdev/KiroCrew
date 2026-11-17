import { ArrowRight, RefreshCw } from 'lucide-react'
import { Trans } from 'react-i18next'

import type { AcpBackendProbe } from '../../api/client'
import { Btn, SendBtn } from '../ui'
import { CopyCommand } from './CopyCommand'
import { fmtList } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** Where the npm install hint sends a host that has no Node.js (npm ships with it). */
const NODE_DOWNLOAD_URL = 'https://nodejs.org/en/download'

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
 *
 * A harness on the wrong release (`found_version`) is installed, so the first
 * line says which release this host has instead of "Install it": the command
 * below replaces it with the verified one.
 *
 * An `npm ...` command gets one more line naming Node.js and linking its
 * download page: npm ships with Node.js, so on a host without it the command
 * fails with "npm: command not found" and nothing else on the card says why.
 * A curl installer or an empty command needs no Node.js, so they get no line.
 */
export function AgentInstallDetail({ name, probe }: { name: string; probe: AcpBackendProbe }) {
  return (
    <>
      <p className="text-[13px] font-medium text-text">
        {probe.found_version
          ? i18nT('components.kiroPrerequisiteGate.agent_wrong_version_detail', {
              name,
              version: probe.found_version,
            })
          : i18nT('components.kiroPrerequisiteGate.agent_install_on_host', { name })}
      </p>
      {probe.install_command ? (
        <>
          <CopyCommand>
            <code>{probe.install_command}</code>
          </CopyCommand>
          {probe.install_command.startsWith('npm ') && (
            <p className="text-[12px] leading-relaxed text-muted">
              <Trans
                i18nKey="components.kiroPrerequisiteGate.agent_install_needs_node"
                components={[
                  // eslint-disable-next-line jsx-a11y/anchor-has-content, jsx-a11y/control-has-associated-label -- `Trans` renders the catalog's <0>Download Node.js</0> text inside it
                  <a
                    key="node"
                    className="text-accent hover:underline focus-ring"
                    href={NODE_DOWNLOAD_URL}
                    rel="noopener noreferrer"
                    target="_blank"
                  />,
                ]}
              />
            </p>
          )}
        </>
      ) : probe.missing_components.length > 0 ? (
        <p className="text-[12px] text-muted">
          {i18nT('components.kiroPrerequisiteGate.agent_missing_components', {
            components: fmtList(probe.missing_components, { style: 'narrow' }),
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
