import { useState } from 'react'
import { Trans } from 'react-i18next'

import SignInCommands, { KiroCliCheckAgain } from '../../components/KiroCliSignInCommands'
import { KiroSignInDialog } from '../../components/KiroSignInDialog'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Card } from '../../components/ui'
import { useKiroPrerequisite } from '../../hooks/useKiroPrerequisite'
import { i18nT } from '../../i18n/t'

/**
 * A signed-out turn that kiro-cli's own sign-in can fix: one line, then the
 * welcome screen's two kiro-cli sign-in commands and its Check again. Status,
 * commands and re-read are the welcome screen's own pieces
 * (`useKiroPrerequisite`, `KiroCliSignInCommands`), and the shared status decides
 * whether the commands show: only while it says kiro-cli is installed and not
 * signed in, the welcome screen's own condition. A failed read, or a status that
 * cannot name the commands, shows the row's own text, which names the same ones.
 *
 * *crewSignIn* is the turn that ran through kiro-cli's relay, which can run on
 * either sign-in: the row also offers the Sign in button that opens the shared
 * dialog, and keeps that button alone once kiro-cli reads signed in.
 */
export default function KiroCliSignInRow({
  fallback,
  crewSignIn = false,
}: {
  fallback: string
  crewSignIn?: boolean
}) {
  const { statusQuery, checkAgain, checking } = useKiroPrerequisite({ poll: false })
  const [open, setOpen] = useState(false)
  const status = statusQuery.data
  // The row's way out. Check sign-in again (the welcome screen's primary) stays the
  // one primary; on the relay Kiro Crew's own sign-in follows it as the outlined
  // fallback after "or". On a phone the two stack, so neither label wraps.
  const actions = (checkable: boolean) => {
    const crew = crewSignIn ? (
      <>
        <Btn
          primary={!checkable}
          className={checkable ? 'rounded-lg px-4 min-h-9 text-sm font-semibold whitespace-nowrap' : undefined}
          onClick={() => setOpen(true)}
          data-testid="kiro-sign-in-open"
        >
          {i18nT(checkable ? 'components.kiroSignInPrompt.use_crew_sign_in' : 'components.kiroSignInPrompt.sign_in')}
        </Btn>
        <KiroSignInDialog open={open} onOpenChange={setOpen} reauth />
      </>
    ) : null
    if (!checkable && !crew) return null
    const check = <KiroCliCheckAgain installed busy={checking} onClick={() => void checkAgain()} />
    return (
      <div className="mt-3 flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:gap-3">
        {checkable && crew ? (
          <Trans
            i18nKey="components.kiroSignInPrompt.check_or_crew"
            components={{ check, or: <span className="text-[13px] text-muted" />, signin: <>{crew}</> }}
          />
        ) : checkable ? check : crew}
      </div>
    )
  }
  const commandsShow = !!status && status.installed && !status.authenticated
  // A status read that failed (a Check again, or the first read) says the check
  // did not run, under the row's own buttons; any last answer stays on screen.
  const recheckFailed =
    statusQuery.isError ? (
      <ErrorNotice
        className="mt-3"
        variant="inline"
        title={i18nT('components.kiroPrerequisiteGate.we_could_not_check_kiro_cli')}
        message={statusQuery.error?.message || i18nT('components.kiroPrerequisiteGate.the_gateway_returned_an_unexpected_error')}
        noHandoffReason="the chat agent runs on the kiro-cli sign-in this row reports as signed out, so a chat it opens fails the same way"
        testId="kiro-cli-sign-in-recheck-error"
      />
    ) : null
  // The row is the failed turn's record, so it never renders nothing. Without a
  // status that names the commands (still loading, a failed read, a viewer's
  // redacted not-installed status), or with one that still reads signed in (the
  // status is only re-probed at boot and on Check again, so a mid-session expiry
  // can leave it stale), it keeps the row's own text, which names the same
  // commands, with Check sign-in again where kiro-cli is installed and, on a relay
  // Kiro Crew's sign-in starts, the Sign in fallback.
  if (!commandsShow) {
    const checkable = !!status?.installed
    return (
      <Card className="mb-0 p-3 self-center w-full max-w-full min-w-0" data-testid="error-card" data-kiro-cli-sign-in="true">
        <p className="m-0 text-[13px] leading-5 text-text-strong" style={{ overflowWrap: 'anywhere' }}>{fallback}</p>
        {actions(checkable)}
        {recheckFailed}
      </Card>
    )
  }
  return (
    <Card className="mb-0 p-3 self-center w-full max-w-full min-w-0" data-testid="error-card" data-kiro-cli-sign-in="true">
      <p className="m-0 text-[13px] leading-5 text-text-strong">
        {i18nT('pages.chat.errorCard.kiro_cli_signed_out')}
      </p>
      <div className="mt-2">
        <SignInCommands status={status} compact />
      </div>
      {actions(true)}
      {recheckFailed}
    </Card>
  )
}
