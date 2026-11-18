import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation } from 'react-router-dom'

import { api } from '../api/client'
import { ACP_BACKEND_KAS, configuredAcpBackend, kasInUse, type AcpBackendConfig } from '../api/acpBackend'
import { i18nT } from '../i18n/t'
import { useOutageFailure } from '../hooks/useOutageFailure'
import { useAppSelector } from '../store'
import ErrorNotice from './ErrorNotice'
import { reportForError } from '../utils/errorReport'
import { KiroSignInDialog } from './KiroSignInDialog'
import { Btn } from './ui'
import { settingsPath } from './settingsPath'
import { KIRO_ACCOUNT_SETTINGS_TAB } from '../pages/developer/kiroSignInLink'
import { useKiroSignInOfferedQuery } from '../pages/developer/useKiroSignInOffered'
import { signInLapsed } from '../pages/developer/kiroSignInState'
import { useKasLoginStatus } from '../hooks/useKasLogin'

/**
 * How often the notice re-reads the stored sign-in.
 * The gateway's renewal tick records a refused renewal in the store, and the
 * status read is a plain store read (no refresh, no network), so a minute
 * bounds how late the notice reports one at the cost of one small read per
 * minute.
 */
export const KIRO_SIGN_IN_NOTICE_POLL_MS = 60_000

/** The lapsed notice's detail, by cause and by who runs as the sign-in. */
const LAPSED_DETAIL = {
  expired: {
    agents: 'components.kiroSignInNotice.expired_detail',
    bots: 'components.kiroSignInNotice.expired_detail_bots',
    account: 'components.kiroSignInNotice.expired_detail_account',
  },
  refresh_rejected: {
    agents: 'components.kiroSignInNotice.refused_detail',
    bots: 'components.kiroSignInNotice.refused_detail_bots',
    account: 'components.kiroSignInNotice.refused_detail_account',
  },
  refresh_rejected_expired: {
    agents: 'components.kiroSignInNotice.refused_expired_detail',
    bots: 'components.kiroSignInNotice.refused_expired_detail_bots',
    account: 'components.kiroSignInNotice.refused_expired_detail_account',
  },
} as const

/**
 * App-level notice for a stored Kiro sign-in that no longer works, mounted
 * above every page beside the crash notice. The sign-in card on Settings >
 * Kiro account says the same thing, but on a page nobody keeps open; without
 * this a lapsed or issuer-refused sign-in stays invisible until a chat turn
 * fails.
 *
 * A banner rather than a top-bar control: the top bar's action groups are at
 * the two-control cap (or already past it from before the cap), so a new
 * control there would grow a capped row. A banner is its own region, and it
 * can say what happened and where the button goes at every width instead of
 * folding to an icon.
 *
 * What it shows, all hidden on Settings > Kiro account itself (the card shows
 * every one of these states next to its own controls). Every shape is an
 * `ErrorNotice` with dismiss, at the same weight, and with the agent hand-off
 * except where the agent cannot run (the two expired shapes below):
 * - The config read FAILED before any good read: nothing says which backend
 *   runs the agents, so the sign-in check cannot start. Its own title says
 *   the settings could not be read and makes no sign-in claim (kiro-cli and
 *   Claude have no Kiro sign-in), with no sign-in button. A failure after a
 *   good read shows nothing: the query keeps that read's backend.
 * - The status read FAILED: a plain-language line saying
 *   what to do, the sign-in button, and the hand-off, which carries the
 *   server's error. Not for a read that failed while the dashboard was
 *   disconnected (a gateway restart): the offline state says that one.
 * - Kiro REFUSED to renew the stored sign-in (`refresh_rejected`, recorded by
 *   the renewal tick when the issuer answers no). Once the access token is
 *   also `expired` (the usual case: the tick only asks inside the refresh
 *   margin) it says the agents cannot run, not that they will stop, and has
 *   no agent hand-off: the agent it would open runs as this same sign-in.
 * - The stored sign-in EXPIRED with nothing to renew it with: every agent's
 *   credential request is now refused, the same outcome as a refusal, so it
 *   has no agent hand-off either.
 * Where the main harness is not KAS and only Bots run on it, both lapsed shapes
 * name the Bots instead of "agents" and keep the hand-off: the main agent runs
 * on another credential, so the chat it opens still works. Where nothing here
 * runs on KAS (`kasInUse`) but the build offers Kiro sign-in, the sign-in still
 * serves the features that run as the Kiro account, so the lapsed shapes name
 * those and keep the hand-off. A lapsed shape needs a stored sign-in, so
 * someone who never signed in is never asked to; a failed status read is an
 * error, shown wherever the status is read. Nothing at all on a build that offers no Kiro sign-in and
 * runs nothing on KAS, with no stored sign-in, or while it works.
 *
 * The lapsed and status-failure shapes carry a "Sign in" button in their
 * footer: signing in again is the fix the user can make. It opens the shared
 * sign-in dialog (`KiroSignInDialog`) in place, so nothing on the page below
 * is left; the agent hand-off (where shown) asks the page's leave guard first.
 * The dialog shows the provider choices, so a banner press is never a one-click
 * browser sign-in, a misclick hazard.
 * Dismissing hides a lapsed notice for its cause
 * until the page reloads (a different cause shows again), and a failed read
 * until a read succeeds again (not on every failed poll).
 */
export default function KiroSignInNotice() {
  const [signInOpen, setSignInOpen] = useState(false)
  const { pathname } = useLocation()
  const [dismissedCause, setDismissedCause] = useState<string | null>(null)
  // `<which read>:<its dataUpdatedAt>` when its failure was dismissed.
  const [dismissedFailure, setDismissedFailure] = useState<string | null>(null)
  const cfgQuery = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const kas = cfgQuery.data !== undefined && kasInUse(cfgQuery.data)
  // The sign-in also serves what runs as the Kiro account on any harness, so a
  // build that offers it reads the stored sign-in whichever harness runs.
  const offeredQuery = useKiroSignInOfferedQuery()
  const offered = offeredQuery.offered === true
  // The shell's own outage surfaces: the offline capsule, and the banner that
  // asks for a token. Same event pair shell/appShell.tsx subscribes to, seeded from the
  // banner element (as in KiroPrerequisiteGate) rather than from api/client.
  const connected = useAppSelector(s => s.dashboard.connected)
  const [authRequired, setAuthRequired] = useState<boolean>(
    () => typeof document !== 'undefined' && document.getElementById('mc-session-expired') !== null,
  )
  useEffect(() => {
    const onRequired = () => setAuthRequired(true)
    const onCleared = () => setAuthRequired(false)
    window.addEventListener('mc-auth-required', onRequired)
    window.addEventListener('mc-auth-cleared', onCleared)
    return () => {
      window.removeEventListener('mc-auth-required', onRequired)
      window.removeEventListener('mc-auth-cleared', onCleared)
    }
  }, [])
  // The shared sign-in entry: a sign-in, sign-out or switch landing anywhere
  // re-reads it, so the notice clears the moment a new sign-in lands.
  const statusQuery = useKasLoginStatus({ enabled: kas || offered, poll: KIRO_SIGN_IN_NOTICE_POLL_MS })
  // A status read that failed because the gateway was down (a restart): the
  // offline state says so, and nothing about the sign-in is known to be wrong.
  const outageFailure = useOutageFailure(
    statusQuery.isError,
    statusQuery.errorUpdatedAt,
    () => statusQuery.refetch({ cancelRefetch: false }),
  )

  if (pathname === settingsPath({ tab: KIRO_ACCOUNT_SETTINGS_TAB })) return null

  // Signing in again is the fix the user can make: the shared sign-in dialog,
  // opened over the stored sign-in, the same one Settings and the chat open.
  const openSignIn = (
    <>
      <Btn primary data-testid="kiro-sign-in-notice-open" onClick={() => setSignInOpen(true)}>
        {i18nT('components.kiroSignInPrompt.sign_in')}
      </Btn>
      <KiroSignInDialog open={signInOpen} onOpenChange={setSignInOpen} reauth />
    </>
  )

  // A config read that failed before any good one: nothing says which backend
  // runs the agents, so the sign-in check cannot start. That is shown, under
  // its own title and with no sign-in claim (on kiro-cli or Claude there is no
  // Kiro sign-in to report). After a good read the query keeps that read's
  // data, so `kas` still holds, the status read goes on, and nothing is lost.
  // While the gateway is offline or asks for a token, those surfaces already
  // say why the read failed and what fixes it, so this one stays out of their
  // way rather than pointing at a reload.
  if (cfgQuery.isError && !cfgQuery.data) {
    if (!connected || authRequired) return null
    // Not re-shown on a later failed retry: dismissed is dismissed until a
    // reload, and a good read ends this branch for good.
    if (dismissedFailure === 'config') return null
    return (
      <ErrorNotice
        className="mx-3 md:mx-6 mt-4 mb-2"
        title={i18nT('components.kiroSignInNotice.config_unavailable')}
        message={i18nT('components.kiroSignInNotice.config_unavailable_detail')}
        report={reportForError(cfgQuery.error)}
        messagePlacement="below"
        actionPlacement="below"
        askAgent
        onDismiss={() => setDismissedFailure('config')}
        testId="kiro-sign-in-config-error"
      />
    )
  }

  const reads = kas || offered
  // Off KAS, only the schema says whether the build offers the sign-in. A
  // schema read that failed with no answer leaves the sign-in unchecked, so it
  // is said, under the same settings-read wording and guards as the config
  // read above. On KAS the status read runs anyway and this stays out.
  if (cfgQuery.data !== undefined && !kas && offeredQuery.offered === undefined && offeredQuery.error) {
    if (!connected || authRequired) return null
    if (dismissedFailure === 'schema') return null
    return (
      <ErrorNotice
        className="mx-3 md:mx-6 mt-4 mb-2"
        title={i18nT('components.kiroSignInNotice.config_unavailable')}
        message={i18nT('components.kiroSignInNotice.config_unavailable_detail')}
        report={reportForError(offeredQuery.error)}
        messagePlacement="below"
        actionPlacement="below"
        askAgent
        onDismiss={() => setDismissedFailure('schema')}
        testId="kiro-sign-in-schema-error"
      />
    )
  }
  if (reads && outageFailure) return null
  // The status read's failure, wherever it is read: nothing then says whether
  // a sign-in is stored or works, and the card is where to look.
  const failedRead = reads && statusQuery.isError
    ? { key: `status:${statusQuery.dataUpdatedAt}`, error: statusQuery.error }
    : null
  if (failedRead) {
    if (dismissedFailure === failedRead.key) return null
    return (
      <ErrorNotice
        className="mx-3 md:mx-6 mt-4 mb-2"
        title={i18nT('components.kasLogin.sign_in_status_unavailable')}
        // What to do, in words: the server's text ("token_store_failed: ...")
        // is no instruction. The hand-off still gets it, with the endpoint and
        // status, from the report the transport pinned to this rejection.
        message={i18nT('components.kiroSignInNotice.status_unavailable_detail')}
        report={reportForError(failedRead.error)}
        messagePlacement="below"
        actionPlacement="below"
        // The way to fix it yourself: sign in again in the shared dialog.
        footer={<div className="mt-1.5">{openSignIn}</div>}
        askAgent
        onDismiss={() => setDismissedFailure(failedRead.key)}
        testId="kiro-sign-in-status-error"
      />
    )
  }

  const status = statusQuery.data
  // `authenticated`: a sign-in is stored, usable or not. Never shown without one.
  if (!reads || !status?.authenticated || !signInLapsed(status)) return null
  // Who runs as the sign-in decides the copy and the hand-off below, so wait
  // for the config rather than show one shape and swap it.
  if (cfgQuery.data === undefined) return null
  // A refusal that has since expired is its own cause: "the agents have
  // stopped" is new information after a dismissed "they will stop".
  const cause = status.refresh_rejected
    ? (status.expired ? 'refresh_rejected_expired' : 'refresh_rejected')
    : 'expired'
  if (dismissedCause === cause) return null

  // Both lapsed states render the same way, at the same weight: in each the
  // agents' credential requests are now refused (or will be), and only signing
  // in again fixes it. An expiry with nothing to renew it is that failure as
  // much as an issuer's refusal is, so it is not dressed down to a warning --
  // a yellow "agents cannot run" under a red "will stop" read backwards.
  // The text says which one happened.
  // Who runs as this sign-in: the agents (main harness on KAS), only the Bots,
  // or neither, so only the features that use the Kiro account. Off the main
  // KAS harness the main agent still runs, so it keeps its hand-off.
  const mainOnKas = cfgQuery.data !== undefined && configuredAcpBackend(cfgQuery.data) === ACP_BACKEND_KAS
  const audience = mainOnKas ? 'agents' : kas ? 'bots' : 'account'
  let title = i18nT('components.kiroSignInNotice.sign_in_expired')
  const detail = i18nT(LAPSED_DETAIL[cause][audience])
  if (cause !== 'expired') {
    // The renewal tick only asks Kiro inside the refresh margin, so by the time
    // a refusal is recorded the access token is usually `expired` too: the
    // agents have already stopped, and "will stop" would be wrong. `usable`
    // cannot tell the two apart (a stored refresh token keeps it true).
    title = i18nT('components.kiroSignInNotice.refused_title')
  }
  const lapsed = {
    className: 'mx-3 md:mx-6 mt-4 mb-2',
    title,
    message: detail,
    messagePlacement: 'below',
    actionPlacement: 'below',
    footer: <div className="mt-1.5">{openSignIn}</div>,
    onDismiss: () => setDismissedCause(cause),
    testId: cause === 'expired' ? 'kiro-sign-in-notice' : 'kiro-sign-in-refused',
  } as const
  // A refusal whose access token still works leaves the agents running until
  // it expires, so one can still look into it.
  if (cause === 'refresh_rejected' || !mainOnKas) return <ErrorNotice {...lapsed} askAgent />
  return (
    <ErrorNotice
      {...lapsed}
      noHandoffReason="on KAS every agent runs as this stored sign-in, which has expired, so the chat the hand-off would open fails on the same credential"
    />
  )
}
