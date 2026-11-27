import { useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { QueryClientContext } from '@tanstack/react-query'
import { Check, Copy, SquareTerminal } from 'lucide-react'

import type { KiroPrerequisiteStatus } from '../api/client'
import { KIRO_PREREQUISITE_KEY } from '../hooks/useKiroPrerequisite'
import { i18nT } from '../i18n/t'
import { appPathname } from '../lib/basePath'
import { useTerminalEnabled } from '../utils/terminalRegistry'
import ErrorNotice from './ErrorNotice'
import { useRunInTerminal } from './RunInTerminalBtn'
import { copyCode } from '../utils/clipboard'
import { Btn } from './ui'

/** The bare sign-in command, for a status not read yet. The gateway serves the
 *  same constant (`kiro_cli_bundle.KIRO_CLI_LOGIN_COMMAND`); the status form wins
 *  because on a desktop install it names the app's own kiro-cli. */
const FALLBACK_LOGIN_COMMAND = 'kiro-cli login'

/**
 * Runs kiro-cli's own sign-in in the dashboard Terminal panel, behind the same
 * confirm dialog every Run in terminal button shows. The model list is the one
 * reader of kiro-cli's own login that a Kiro Crew sign-in does not cover, and
 * kiro-cli has no sign-in the dashboard can drive itself, so this is the way in
 * that needs no typing.
 *
 * The command is the prerequisite status the welcome screen already read at
 * boot (`login_command`), read from the cache rather than fetched again (and
 * read through the context, so a mount with no query client gets the bare
 * command instead of a throw). The
 * caller mounts `dialog` OUTSIDE any popover it closes first: a picker that
 * unmounts would take the dialog with it.
 */
export function useKiroCliSignInInTerminal(): { askToSignIn: () => void; dialog: ReactNode; command: string } {
  const queryClient = useContext(QueryClientContext)
  const status = queryClient?.getQueryData<KiroPrerequisiteStatus>(KIRO_PREREQUISITE_KEY)
  const config = queryClient?.getQueryData<{ dashboard?: { terminal?: { reuse_current?: boolean } } }>(['kirocrewConfig'])
  const command = status?.login_command || FALLBACK_LOGIN_COMMAND
  const { askToRun, dialog } = useRunInTerminal({
    code: command,
    willCopy: config?.dashboard?.terminal?.reuse_current === true,
  })
  return { askToSignIn: askToRun, dialog, command }
}

/**
 * Whether a Run here would reach a Terminal panel that runs it on this machine:
 * the terminal is on, this window has a dock (embed and popout windows have
 * none, so nothing answers the request), and the chat is known to run here.
 * *runsHere* is the chat's placement (`useSlotRunsHere`): `false` on another
 * machine and `undefined` while unread are both refused by the run, so only
 * `true` counts. A host outside every chat (Settings) passes `true`.
 */
export function useKiroCliCanRunHere(runsHere: boolean | undefined): boolean {
  const terminalEnabled = useTerminalEnabled()
  const path = appPathname()
  const hasDock = !path.startsWith('/embed/') && !path.startsWith('/popout/')
  return terminalEnabled && hasDock && runsHere === true
}

/** What a sign-in row needs from its host: the run, the command it runs, and
 *  whether a terminal here can run it. */
export interface KiroCliSignIn {
  run: () => void
  command: string
  /** False where no Terminal panel would answer (the terminal is off, an
   *  embedded chat with no dock) or the chat runs on another machine, which
   *  the run refuses. The row then copies the command instead. */
  canRun: boolean
}

/** The button that starts that sign-in, or copies its command where no terminal
 *  here can run it. */
export function KiroCliSignInButton({ signIn }: { signIn: KiroCliSignIn }) {
  if (!signIn.canRun) return <KiroCliCopySignIn command={signIn.command} />
  const onClick = signIn.run
  return (
    <Btn type="button" className="shrink-0 inline-flex items-center gap-1.5" onClick={onClick} data-testid="kiro-cli-sign-in-terminal">
      <SquareTerminal className="lucide-inline" aria-hidden />
      {i18nT('components.kiroCliSignInInTerminal.sign_in')}
    </Btn>
  )
}

/**
 * The model picker's row for a list that kiro-cli's sign-in holds back: why
 * only the fallback is listed, and the button that signs kiro-cli in.
 */
export function KiroCliModelsSignInNotice({ signIn }: { signIn: KiroCliSignIn }) {
  return (
    // Stacked: the picker is about 340px wide, and the message squeezed beside
    // a labelled button wrapped one or two words per line.
    <div className="flex shrink-0 flex-col items-start gap-1.5 px-1.5 py-1" data-testid="kiro-cli-models-signed-out">
      {/* No hand-off: the composer under this picker may hold an unsent draft. */}
      <ErrorNotice
        className="min-w-0 self-stretch"
        variant="inline"
        message={i18nT('components.kiroCliSignInInTerminal.models_signed_out')}
      />
      <KiroCliSignInButton signIn={signIn} />
    </div>
  )
}

/** The copy fallback: a labelled button, so the row says what it does without a
 *  tooltip, and a notice when the clipboard refuses. */
function KiroCliCopySignIn({ command }: { command: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<ReturnType<typeof setTimeout>>()
  useEffect(() => () => clearTimeout(timer.current), [])
  const copy = async () => {
    const ok = await copyCode(command)
    setState(ok ? 'copied' : 'failed')
    clearTimeout(timer.current)
    if (ok) timer.current = setTimeout(() => setState('idle'), 1500)
  }
  return (
    <>
      <Btn type="button" className="shrink-0 inline-flex items-center gap-1.5" onClick={() => void copy()} data-testid="kiro-cli-sign-in-copy">
        {state === 'copied' ? <Check className="lucide-inline" aria-hidden /> : <Copy className="lucide-inline" aria-hidden />}
        {state === 'copied'
          ? i18nT('components.monacoCodeBlock.copied')
          : i18nT('components.kiroCliSignInInTerminal.copy_sign_in')}
      </Btn>
      {/* The notice says to select the command, so the command is put on screen:
          `select-all` takes it in one click, and wrapped (never clipped) as in
          RunYourselfCard, so a long bundled path shows every character. */}
      {/* No hand-off: this button sits beside the composer's unsent draft (model
          picker) and Settings ▸ Chat's `localRoleOther` / `localBudget` /
          `localKeepChars` drafts; a hand-off navigates away and loses them. */}
      {state === 'failed' && (
        <>
          <ErrorNotice variant="inline" message={i18nT('components.runYourselfCard.copy_failed')} />
          <code className="self-stretch select-all p-1.5 rounded-md bg-bg border border-border text-[12px] font-mono whitespace-pre-wrap [overflow-wrap:anywhere] text-text">{command}</code>
        </>
      )}
    </>
  )
}
