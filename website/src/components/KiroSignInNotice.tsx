import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation, useNavigate } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'

import { api } from '../api/client'
import type { AcpBackendConfig } from '../api/acpBackend'
import { i18nT } from '../i18n/t'
import { useAppSelector } from '../store'
import ErrorNotice from './ErrorNotice'
import { reportForError } from '../utils/errorReport'
import { useGuardedLeave } from './NavigationLeaveGuard'
import { Btn } from './ui'
import { settingsPath } from './settingsPath'
import { AGENT_HARNESS_SETTINGS_TAB, KIRO_SIGN_IN_BACKEND, KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
import { signInLapsed } from '../pages/developer/kiroSignInState'

// Same cache key the sign-in flow and card read status under
// (KasLoginGate.QUERY_KEY): a sign-in, sign-out or switch landing there
// re-reads this one entry, so the notice clears the moment a new sign-in lands
// instead of waiting for its own next poll.
const STATUS_QUERY_KEY = ['kas-login'] as const

/**
 * How often the notice re-reads the stored sign-in while KAS is the backend.
 * The gateway's renewal tick records a refused renewal in the store, and the
 * status read is a plain store read (no refresh, no network), so a minute
 * bounds how late the notice reports one at the cost of one small read per
 * minute.
 */
export const KIRO_SIGN_IN_NOTICE_POLL_MS = 60_000

/**
 * App-level notice for a stored Kiro sign-in that no longer works, mounted
 * above every page beside the crash notice. The sign-in card on Settings >
 * Agent Harness says the same thing, but on a page nobody keeps open; without
 * this a lapsed or issuer-refused sign-in stays invisible until a chat turn
 * fails.
 *
 * A banner rather than a top-bar control: the top bar's action groups are at
 * the two-control cap (or already past it from before the cap), so a new
 * control there would grow a capped row. A banner is its own region, and it
 * can say what happened and where the button goes at every width instead of
 * folding to an icon.
 *
 * What it shows, all hidden on Settings > Agent Harness itself (the card shows
 * every one of these states next to its own controls). Every shape is an
 * `ErrorNotice` with dismiss, at the same weight, and with the agent hand-off
 * except where the agent cannot run (the two expired shapes below):
 * - The config read FAILED before any good read: nothing says which backend
 *   runs the agents, so the sign-in check cannot start. Its own title says
 *   the settings could not be read and makes no sign-in claim (kiro-cli and
 *   Claude have no Kiro sign-in), with no sign-in button. A failure after a
 *   good read shows nothing: the query keeps that read's backend.
 * - The status read FAILED on a KAS gateway: a plain-language line saying
 *   what to do, the sign-in button, and the hand-off, which carries the
 *   server's error.
 * - Kiro REFUSED to renew the stored sign-in (`refresh_rejected`, recorded by
 *   the renewal tick when the issuer answers no). Once the access token is
 *   also `expired` (the usual case: the tick only asks inside the refresh
 *   margin) it says the agents cannot run, not that they will stop, and has
 *   no agent hand-off: the agent it would open runs as this same sign-in.
 * - The stored sign-in EXPIRED with nothing to renew it with: every agent's
 *   credential request is now refused, the same outcome as a refusal, so it
 *   has no agent hand-off either.
 * Nothing at all while KAS is not the selected backend (the one backend that
 * runs agents as this identity), with no stored sign-in, or while it works. A
 * gateway on another backend never reads the status.
 *
 * The lapsed and status-failure shapes carry the "Open sign-in settings"
 * button in their footer: signing in again is the fix the user can make.
 *
 * The hand-offs: the notice holds no input, and both the agent hand-off (where
 * shown) and
 * the sign-in button ask the page's leave guard before navigating, so a draft
 * on the page below is not lost.
 *
 * The sign-in button opens the card rather than starting a sign-in: the card
 * is where the provider is chosen, and a one-click browser sign-in from a
 * banner is a misclick hazard. Dismissing hides a lapsed notice for its cause
 * until the page reloads (a different cause shows again), and a failed read
 * until a read succeeds again (not on every failed poll).
 */
export default function KiroSignInNotice() {
  const navigate = useNavigate()
  const leave = useGuardedLeave()
  const { pathname } = useLocation()
  const [dismissedCause, setDismissedCause] = useState<string | null>(null)
  // `<which read>:<its dataUpdatedAt>` when its failure was dismissed.
  const [dismissedFailure, setDismissedFailure] = useState<string | null>(null)
  const cfgQuery = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const kas = cfgQuery.data?.agent?.acp_backend === KIRO_SIGN_IN_BACKEND
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
  const statusQuery = useQuery({
    queryKey: STATUS_QUERY_KEY,
    queryFn: api.kasLoginStatus,
    enabled: kas,
    refetchInterval: KIRO_SIGN_IN_NOTICE_POLL_MS,
  })

  if (pathname === settingsPath({ tab: AGENT_HARNESS_SETTINGS_TAB })) return null

  const openSignIn = (
    <Btn
      primary
      data-testid="kiro-sign-in-notice-open"
      className="min-w-0 max-w-full text-left"
      onClick={() => leave(() => navigate(KIRO_SIGN_IN_PATH), KIRO_SIGN_IN_PATH)}
    >
      <span className="min-w-0">{i18nT('components.kiroSignInNotice.open_sign_in')}</span>
      <ArrowRight size={14} className="shrink-0" aria-hidden="true" />
    </Btn>
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

  // The status read's failure, on KAS only (the only backend that reads it).
  const failedRead = kas && statusQuery.isError
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
        // The way to fix it yourself: the sign-in card shows the same read
        // with its own controls, and this notice is hidden there.
        footer={<div className="mt-1.5">{openSignIn}</div>}
        askAgent
        onDismiss={() => setDismissedFailure(failedRead.key)}
        testId="kiro-sign-in-status-error"
      />
    )
  }

  const status = statusQuery.data
  if (!kas || !status?.authenticated || !signInLapsed(status)) return null
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
  let title = i18nT('components.kiroSignInNotice.sign_in_expired')
  let detail = i18nT('components.kiroSignInNotice.expired_detail')
  if (cause !== 'expired') {
    // The renewal tick only asks Kiro inside the refresh margin, so by the time
    // a refusal is recorded the access token is usually `expired` too: the
    // agents have already stopped, and "will stop" would be wrong. `usable`
    // cannot tell the two apart (a stored refresh token keeps it true).
    title = i18nT('components.kiroSignInNotice.refused_title')
    detail = cause === 'refresh_rejected_expired'
      ? i18nT('components.kiroSignInNotice.refused_expired_detail')
      : i18nT('components.kiroSignInNotice.refused_detail')
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
  if (cause === 'refresh_rejected') return <ErrorNotice {...lapsed} askAgent />
  return (
    <ErrorNotice
      {...lapsed}
      noHandoffReason="on KAS every agent runs as this stored sign-in, which has expired, so the chat the hand-off would open fails on the same credential"
    />
  )
}
