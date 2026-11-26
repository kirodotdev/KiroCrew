import { ExternalLink } from 'lucide-react'

import type { AcpBackendProbe, KiroPrerequisiteStatus } from '../../api/client'
import { ACP_BACKEND_KIRO } from '../../api/acpBackend'
import { AgentInstallDetail } from './AgentDetail'
import { KiroInstallCommands } from './KiroInstallCommands'
import { i18nT } from '../../i18n/t'

/** Kiro's own install page: every install route the one-liner does not cover. */
export const KIRO_CLI_INSTALL_DOCS_URL = 'https://kiro.dev/cli/'

/**
 * The "not installed" detail for one harness row, shared by Settings > Agent
 * Harness and first-run setup. Kiro CLI has no server one-liner, so it gets the
 * same platform install commands first-run setup has always shown, plus its
 * install page; every other harness shows the probe's own command.
 */
export function HarnessInstallDetail({ value, name, probe, prerequisite }: {
  value: string
  name: string
  probe: AcpBackendProbe
  /** The gateway's platform facts; without them Kiro CLI falls back to its page. */
  prerequisite?: Pick<KiroPrerequisiteStatus, 'platform' | 'unzip_missing'>
}) {
  if (value === ACP_BACKEND_KIRO && prerequisite && !probe.install_command) {
    return (
      <>
        <KiroInstallCommands platform={prerequisite.platform} unzipMissing={prerequisite.unzip_missing === true} />
        <KiroSetupLink />
      </>
    )
  }
  return (
    <AgentInstallDetail
      name={name}
      probe={probe}
      installGuideUrl={value === ACP_BACKEND_KIRO ? KIRO_CLI_INSTALL_DOCS_URL : undefined}
    />
  )
}

/** Kiro's install page, with the same words and mark as `AgentInstallDetail`'s link. */
function KiroSetupLink() {
  return (
    <a
      className="inline-flex items-center gap-1 self-start text-[13px] text-accent hover:underline focus-ring"
      href={KIRO_CLI_INSTALL_DOCS_URL}
      rel="noopener noreferrer"
      target="_blank"
    >
      {i18nT('components.kiroPrerequisiteGate.open_kiro_cli_setup')}
      <ExternalLink size={12} aria-hidden />
    </a>
  )
}
