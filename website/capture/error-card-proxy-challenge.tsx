/**
 * Evidence for the error row a proxy auth challenge produces.
 *
 * BEFORE: the proxy's HTML sign-in page is dropped rather than rendered, so the
 * card falls through to the bare status — no cause, no next step.
 *
 * AFTER: the same refusal, recognised, naming the proxy and the recovering action.
 *
 * REJECTED: the pre-existing gateway string, whose terminal-and-banner remedy
 * cannot fix a proxy lapse. Shown so the two remedies can be compared.
 *
 * Strings come from the catalog via `i18nT`, so the frame proves each key resolves.
 *
 *   ?theme=dark|light
 */
import { Fragment } from 'react'
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import { i18nT } from '../src/i18n/t'
import { edgeChallengeMessage } from '../src/api/edgeAuthChallenge'
import { ErrorCard } from '../src/pages/chat/ErrorCard'
import { EPISODES } from './error-card-proxy-challenge.episodes.mjs'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

/** What the card showed before: the HTML challenge page carries no message to unwrap. */
const BEFORE_TEXT = 'HTTP 403'

/**
 * The two proxy strings come from the shipped mapping rather than being re-interpolated
 * here, so the sheet cannot word them differently from production -- and the framed one
 * carries THIS document's real origin, which is what makes the address render as a link
 * instead of inert text.
 */
const CHALLENGED_TEXT = edgeChallengeMessage('challenged') ?? ''
const FRAMED_TEXT = edgeChallengeMessage('framed') ?? ''
const REJECTED_TEXT = i18nT('api.client.session_expired_sign_in_again')

function Label({ children }: { children: string }) {
  return (
    <div
      style={{
        fontSize: 11,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        opacity: 0.55,
        margin: '18px 0 6px',
        fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      }}
    >
      {children}
    </div>
  )
}

/**
 * One entry per shared episode id, so the scene cannot render a set the runner does
 * not assert. Each card is rendered as PRODUCTION renders it, not with a stub
 * handler: a proxy challenge sets `authRequired`, so no retry is offered on one; the
 * pre-fix bare status was retryable, and the gateway's own auth string offers the
 * sign-in route instead. A fabricated affordance here is a false claim about the UI.
 */
const SHEET: Record<string, { label: string; card: React.ReactElement }> = {
  before: {
    label: "BEFORE — the proxy's sign-in page is dropped, leaving the bare status",
    card: <ErrorCard content={BEFORE_TEXT} onContinue={() => {}} />,
  },
  challenged: {
    label: 'CHALLENGED — reload leads, and the lapse is offered as a condition to settle',
    card: <ErrorCard content={CHALLENGED_TEXT} />,
  },
  framed: {
    label: 'FRAMED — reloading the host cannot complete a sign-in inside a panel',
    card: <ErrorCard content={FRAMED_TEXT} />,
  },
  rejected: {
    label: 'REJECTED — the gateway string, whose remedy cannot fix a proxy lapse',
    card: <ErrorCard content={REJECTED_TEXT} onOpenSignIn={() => {}} />,
  },
}

function Scene() {
  return (
    <div
      data-capture-root
      style={{
        maxWidth: 760,
        margin: '0 auto',
        padding: '20px 24px 28px',
        background: 'var(--bg)',
        color: 'var(--text)',
      }}
    >
      {EPISODES.map(id => {
        const { label, card } = SHEET[id]
        return (
          <Fragment key={id}>
            <Label>{label}</Label>
            <div data-episode={id} style={{ display: 'flex', flexDirection: 'column' }}>
              {card}
            </div>
          </Fragment>
        )
      })}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
