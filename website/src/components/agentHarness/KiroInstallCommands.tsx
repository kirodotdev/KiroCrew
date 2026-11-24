import { i18nT } from '../../i18n/t'
import { KiroUnzipPrerequisite } from '../KiroUnzipPrerequisite'
import { CopyCommand } from './CopyCommand'

/**
 * The platform label when the gateway named one this screen can NAME, else ''.
 *
 * The gateway reports one of these three for a host it recognises, and something
 * else when it does not or will not say: `"gateway"` for a non-owner, `"Unknown"`
 * for an OS it has no label for, or the raw `sys.platform` value. Anything
 * outside the set renders as no platform, so the intro never reads "Unknown
 * gateway host" while the Kiro card, one screen region below, admits it does not
 * know the OS. Written as comparisons rather than a lookup table: these are the
 * gateway's wire labels, and the i18n gate reads a string table as copy.
 */
export function knownPlatform(platform: string | undefined): string {
  return platform === 'Windows' || platform === 'macOS' || platform === 'Linux' ? platform : ''
}

/**
 * The install command for Kiro CLI, for the gateway's platform.
 *
 * Kiro Crew still does not RUN it: the command is shown for the user to paste on
 * the gateway host, and Kiro's own setup page stays one click away for every
 * other install route (RPM, AppImage, musl). The commands are `<code>` children,
 * never catalog values — a translated command cannot be typed.
 */
export function KiroInstallCommands({ platform, unzipMissing }: { platform: string; unzipMissing: boolean }) {
  const windows = platform === 'Windows'
  const unix = platform === 'macOS' || platform === 'Linux'
  const known = knownPlatform(platform) !== ''
  // The prerequisite and the installer read as two ordered steps: "First, install
  // unzip", then this line.
  const unzipFirst = platform === 'Linux' && unzipMissing
  return (
    <div className="space-y-3">
      {unzipFirst && <KiroUnzipPrerequisite />}
      <p className="text-[13px] leading-relaxed text-text">
        {unzipFirst
          ? i18nT('components.kiroPrerequisiteGate.install_kiro_cli_then_run_on_host', { platform })
          : known
          ? i18nT('components.kiroPrerequisiteGate.install_kiro_cli_run_on_host', { platform })
          : i18nT('components.kiroPrerequisiteGate.install_kiro_cli_run_for_platform')}
      </p>
      {!windows && (
        <div>
          {!known && (
            <p className="text-[12px] font-medium text-muted">
              {i18nT('components.kiroPrerequisiteGate.install_command_macos_linux')}
            </p>
          )}
          <CopyCommand>
            <code>curl -fsSL https://cli.kiro.dev/install | bash</code>
          </CopyCommand>
        </div>
      )}
      {!unix && (
        <div>
          <p className="text-[12px] font-medium text-muted">
            {i18nT('components.kiroPrerequisiteGate.install_command_windows')}
          </p>
          <CopyCommand>
            <code>irm &apos;https://cli.kiro.dev/install.ps1&apos; | iex</code>
          </CopyCommand>
        </div>
      )}
    </div>
  )
}
