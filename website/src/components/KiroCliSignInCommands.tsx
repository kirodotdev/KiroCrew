import { RefreshCw } from 'lucide-react'

import type { KiroPrerequisiteStatus } from '../api/client'
import { i18nT } from '../i18n/t'
import { CopyCommand } from './agentHarness'
import { SendBtn } from './ui'

/** The fields of `GET /api/kiro-prerequisite` the commands are drawn from. */
export type SignInCommandsStatus = Pick<
  KiroPrerequisiteStatus,
  'login_command' | 'sso_login_command' | 'bundled_cli' | 'platform'
>

/**
 * Split two sign-in commands into the run they share and the tails that differ.
 *
 * On a desktop install the resolved kiro-cli is the app's bundled copy, so both
 * commands open with the same ~100-character quoted absolute path and differ
 * only after `login`. Rendered as two full lines they read as one command shown
 * twice; rendering the shared run muted and only the tail at full weight puts
 * the choice where the eye lands. The cut falls on the last space inside the
 * common prefix so a tail always starts at a word boundary (`login` /
 * `login --use-device-flow ...`). The copied text is still the whole command,
 * read back from the DOM.
 */
function splitSharedCommandPrefix(a: string, b: string): [string, string, string] {
  let i = 0
  while (i < a.length && i < b.length && a[i] === b[i]) i++
  const cut = a.lastIndexOf(' ', i - 1) + 1
  return [a.slice(0, cut), a.slice(cut), b.slice(cut)]
}

/**
 * The two sign-in commands, rendered VERBATIM from the backend constants, never
 * catalog values: a translated command cannot be typed. Shown only once a CLI
 * exists to sign into — before that the install step owns the screen. Kiro Crew
 * does not run them; the footer's Check again reads the result.
 *
 * BOTH tiers are offered, because the sign-in page the bare command opens
 * presents a free Builder ID as a peer of organization SSO: a user on an SSO
 * plan who picks the wrong one authenticates successfully and only discovers
 * the mismatch later, as missing models. Naming the tier here makes it a
 * decision instead of a guess. Kiro Crew does not detect which one applies —
 * that would mean inspecting the host's identity configuration — so the copy
 * describes the choice and lets the user make it.
 *
 * Both commands are click-to-copy, like every other command on this screen: the
 * desktop app's bundled kiro-cli is served as a quoted absolute path that nobody
 * should have to retype into a terminal, and one typo restarts the loop. That
 * path is the same in both boxes, so on a bundled install it is rendered muted,
 * the differing tail carries the weight, and ONE hint before both boxes explains
 * what the path is before a first-time reader meets it.
 */
/**
 * The executable the desktop app stages as its own Kiro CLI, as the bundled
 * sign-in commands name it (`kiro_cli_bundle.BUNDLED_KIRO_CLI_ENTRY` and
 * `BUNDLED_KIRO_CLI_WINDOWS_ENTRY` on the gateway). Code constants, not catalog
 * values, so a translation cannot change the file name the hint points at.
 */
const BUNDLED_KIRO_CLI_ENTRY = 'kiro-cli-chat'
const BUNDLED_KIRO_CLI_WINDOWS_ENTRY = 'kiro-cli.exe'

export default function SignInCommands({
  status,
  compact = false,
}: {
  status: SignInCommandsStatus
  /** Only the hint, the two labels and the two commands: the chat row's form. */
  compact?: boolean
}) {
  const [shared, personalTail, ssoTail] = status.bundled_cli
    ? splitSharedCommandPrefix(status.login_command, status.sso_login_command)
    : ['', status.login_command, status.sso_login_command]
  const prefix = shared ? <span className="text-muted">{shared}</span> : null
  // On Windows the muted run is a PowerShell wrapper around the bundled
  // executable, so the hint says what that wrapper is instead of calling it a path.
  const windows = status.platform === 'Windows'
  return (
    <div className={compact ? 'space-y-2' : 'mt-3 space-y-3'}>
      {status.bundled_cli && (
        <p className="text-[12px] leading-relaxed text-muted">
          {windows
            ? i18nT('components.kiroPrerequisiteGate.sign_in_bundled_hint_windows', {
              binary: BUNDLED_KIRO_CLI_WINDOWS_ENTRY,
            })
            : i18nT('components.kiroPrerequisiteGate.sign_in_bundled_hint', {
              binary: BUNDLED_KIRO_CLI_ENTRY,
            })}
        </p>
      )}
      <div>
        <p className="text-[13px] font-medium text-text">
          {i18nT('components.kiroPrerequisiteGate.sign_in_personal_label')}
        </p>
        <CopyCommand>
          <code>
            {prefix}
            {personalTail}
          </code>
        </CopyCommand>
      </div>
      <div>
        <p className="text-[13px] font-medium text-text">
          {i18nT('components.kiroPrerequisiteGate.sign_in_sso_label')}
        </p>
        <CopyCommand>
          <code>
            {prefix}
            {ssoTail}
          </code>
        </CopyCommand>
        {!compact && (
          <p className="mt-1.5 text-[12px] leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.sign_in_sso_hint')}
          </p>
        )}
      </div>
      {!compact && (
        <p className="text-[12px] leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.sign_in_method_note')}
        </p>
      )}
    </div>
  )
}

/**
 * The Kiro CLI card's "Check again": re-reads the status with a host probe
 * (`useKiroPrerequisite().checkAgain`). Its label names what it re-checks: the
 * sign-in once kiro-cli is installed, kiro-cli itself before.
 */
export function KiroCliCheckAgain({
  installed,
  busy,
  onClick,
}: {
  installed: boolean
  busy: boolean
  onClick: () => void
}) {
  return (
    <SendBtn
      type="button"
      className="inline-flex items-center gap-1.5"
      disabled={busy}
      onClick={onClick}
    >
      <RefreshCw className={`lucide-inline ${busy ? 'animate-spin' : ''}`} />
      {installed
        ? i18nT('components.kiroPrerequisiteGate.check_sign_in_again')
        : i18nT('components.kiroPrerequisiteGate.check_again_for_kiro_cli')}
    </SendBtn>
  )
}
