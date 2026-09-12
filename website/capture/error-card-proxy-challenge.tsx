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
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import { i18nT } from '../src/i18n/t'
import { ErrorCard } from '../src/pages/chat/ErrorCard'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

/** What the card showed before: the HTML challenge page carries no message to unwrap. */
const BEFORE_TEXT = 'HTTP 403'

const AFTER_TEXT = i18nT('api.client.proxy_session_expired_reload')
const BLOCKED_TEXT = i18nT('api.client.proxy_refused_no_sign_in')
const FRAMED_TEXT = i18nT('api.client.proxy_session_expired_framed', {
  origin: 'https://crew-remote-3.internal:8443',
})
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
 * Each episode renders the card as PRODUCTION renders it, not with a stub handler.
 *
 * A proxy challenge sets `authRequired`, so no retry is offered on one; the pre-fix
 * bare status was retryable, and the gateway's own auth string offers the sign-in
 * route instead. Passing a continue handler to every episode drew a Resume button
 * beside copy naming a different action — and the frame is the evidence, so a
 * fabricated affordance here is a false claim about the UI.
 */
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
      <Label>BEFORE — the proxy's sign-in page is dropped, leaving the bare status</Label>
      <div data-episode="before" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={BEFORE_TEXT} onContinue={() => {}} />
      </div>
      <Label>AFTER — the refusal names the proxy and the action that recovers</Label>
      <div data-episode="after" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={AFTER_TEXT} />
      </div>
      <Label>BLOCK PAGE — same 403 + HTML, no sign-in link: named, not diagnosed as a lapse</Label>
      <div data-episode="blocked" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={BLOCKED_TEXT} />
      </div>
      <Label>FRAMED — reloading the host cannot complete a sign-in inside a panel</Label>
      <div data-episode="framed" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={FRAMED_TEXT} />
      </div>
      <Label>REJECTED — the gateway string, whose remedy cannot fix a proxy lapse</Label>
      <div data-episode="rejected" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={REJECTED_TEXT} onOpenSignIn={() => {}} />
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
