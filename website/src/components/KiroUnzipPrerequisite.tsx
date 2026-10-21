import { CopyCommand } from './agentHarness'
import { i18nT } from '../i18n/t'

/**
 * The `unzip` the Linux Kiro CLI installer needs, named above its command.
 *
 * The installer refuses a host without `unzip` before it downloads anything, and
 * a stock Ubuntu image has none, so without this the user's first paste fails.
 * Rendered only when the gateway reports `unzip_missing`: the gateway asks the
 * installer's own question on the host the command runs on, and **Check again**
 * clears it once `unzip` is installed. The package commands are `<code>`
 * children, never catalog values, because a translated command cannot be typed.
 */
export function KiroUnzipPrerequisite() {
  return (
    <div data-testid="kiro-gate-unzip-missing">
      <p className="text-[13px] leading-relaxed text-text">
        {i18nT('components.kiroPrerequisiteGate.unzip_missing_install_first')}
      </p>
      <p className="mt-2 text-[12px] font-medium text-muted">
        {i18nT('components.kiroPrerequisiteGate.unzip_command_debian_ubuntu')}
      </p>
      <CopyCommand>
        <code>sudo apt install unzip</code>
      </CopyCommand>
      <p className="mt-2 text-[12px] font-medium text-muted">
        {i18nT('components.kiroPrerequisiteGate.unzip_command_fedora_rhel')}
      </p>
      <CopyCommand>
        <code>sudo dnf install unzip</code>
      </CopyCommand>
    </div>
  )
}
