/**
 * Isolated capture entry for the live-browser card (RFC: live browser in chat,
 * section 5). The card's state comes from `browser_live` websocket events and
 * binary frames; here both are fed straight into the live-browser store, with a
 * frame drawn in-browser (a small page mock, JPEG-encoded by a canvas) so the
 * scene needs no gateway, no relay and no Chrome. The card, its views, the
 * canvas painter, the stylesheet and the theme tokens are the shipped ones.
 *
 * Scenes: ?scene=allow | live | hover | takeover | done | login | error
 * Theme:  &theme=dark|light
 *
 * `hover` is the live card with the hover overlay forced on (the harness adds a
 * class the overlay also answers to), since a headless pointer hover is not a
 * reliable thing to screenshot.
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { browserLiveStore } from '../src/hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../src/hooks/useBrowserLive.frame'
import BrowserLiveCard from '../src/pages/chat/BrowserLiveCard'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'live'
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

const S = 'slot:capture'
const STEPS = ['Open jspaint.app', 'Pick the rectangle tool', 'Draw the walls', 'Draw the roof', 'Door and windows', 'Screenshot']

// The card's writes are stubbed: nothing here has a gateway to post to.
browserLiveStore.__setWritesForTests({
  ack: async () => ({}), visibility: async () => ({}), claim: async () => ({}),
  release: async () => ({}), input: async () => ({}), open: async () => ({}),
})

/** A 1280x800 "page": the paint program's chrome and a little house. */
async function mockFrame(seq: number, withSun: boolean): Promise<DecodedBrowserFrame> {
  const c = document.createElement('canvas')
  c.width = 1280
  c.height = 800
  const g = c.getContext('2d')!
  g.fillStyle = '#808080'
  g.fillRect(0, 0, 1280, 800)
  g.fillStyle = '#d4d0c8'
  g.fillRect(0, 0, 1280, 28)
  g.fillRect(0, 28, 70, 772)
  g.fillStyle = '#ffffff'
  g.fillRect(80, 36, 900, 500)
  g.fillStyle = '#ff0000'
  g.fillRect(300, 250, 320, 220)
  g.fillStyle = '#0000ff'
  g.beginPath(); g.moveTo(280, 250); g.lineTo(460, 120); g.lineTo(640, 250); g.closePath(); g.fill()
  g.fillStyle = '#80ffff'
  g.fillRect(330, 290, 70, 60); g.fillRect(520, 290, 70, 60)
  g.fillStyle = '#804000'
  g.fillRect(430, 350, 60, 120)
  if (withSun) { g.fillStyle = '#ffff00'; g.beginPath(); g.arc(860, 130, 50, 0, Math.PI * 2); g.fill() }
  const blob: Blob = await new Promise(r => c.toBlob(b => r(b!), 'image/jpeg', 0.7))
  return {
    header: { type: 'browser_frame', session_key: S, seq, w: 1280, h: 800, mask: [] },
    jpeg: new Uint8Array(await blob.arrayBuffer()),
    receivedAt: 1000 + seq * 120,
  }
}

const base = {
  session_key: S, turn_id: 'turn-1', url: 'https://jspaint.app/', title: 'untitled - Paint',
  browser: 'user_chrome', headless: false, rung: 0, fps: 58,
}
const steps = (doneUpTo: number) => STEPS.map((label, i) => ({ label, status: i < doneUpTo ? 'done' : i === doneUpTo ? 'active' : 'pending' }))

async function seed() {
  switch (scene) {
    case 'allow':
      browserLiveStore.applyEvent({ ...base, state: 'waiting_allow', title: '', steps: [] })
      return false
    case 'done':
      browserLiveStore.applyEvent({ ...base, state: 'live', steps: steps(5), step: { index: 5, label: 'Screenshot' } })
      for (let i = 1; i <= 6; i++) browserLiveStore.applyFrame(await mockFrame(i, i > 3))
      browserLiveStore.applyEvent({ ...base, state: 'done', steps: STEPS.map(label => ({ label, status: 'done' })) })
      return true
    case 'error':
      browserLiveStore.applyEvent({ ...base, state: 'live', steps: steps(2), step: { index: 2, label: 'Draw the walls' } })
      browserLiveStore.applyFrame(await mockFrame(1, false))
      browserLiveStore.applyEvent({ ...base, state: 'error', detail: 'Chrome closed the remote debugging port' })
      return true
    case 'login':
      browserLiveStore.applyEvent({ ...base, state: 'login_needed', url: 'https://github.com/login', title: 'Sign in to GitHub', steps: steps(1), step: { index: 1, label: 'Open the sign-in page' } })
      browserLiveStore.applyFrame(await mockFrame(1, false))
      return true
    case 'takeover':
      browserLiveStore.applyEvent({ ...base, state: 'in_control', steps: steps(3), step: { index: 3, label: 'Draw the roof' } })
      browserLiveStore.applyFrame(await mockFrame(1, true))
      return true
    default:
      browserLiveStore.applyEvent({ ...base, state: 'live', steps: steps(3), step: { index: 3, label: 'Click · polygon tool → draw the roof' } })
      browserLiveStore.applyFrame(await mockFrame(1, false))
      return true
  }
}

function App({ running }: { running: boolean }) {
  return (
    <div data-capture-root style={{ padding: 24, background: 'var(--bg)', minHeight: '100vh', ['--mc-content-width' as string]: '760px' }}>
      <BrowserLiveCard sessionKey={S} anchorKey="call:capture" anchorTs={Date.now()} running={running} />
    </div>
  )
}

const hadFrame = await seed()
const running = scene !== 'done' && scene !== 'error'
createRoot(document.getElementById('root')!).render(<MemoryRouter><App running={running} /></MemoryRouter>)

// A frame drawn once the canvas has mounted and subscribed: the first frame is
// already in the ring, so the painter picks it up on subscribe; this second one
// marks the scene ready for the capture script.
if (hadFrame) {
  await new Promise(r => setTimeout(r, 300))
  if (scene === 'hover') document.querySelector('[data-testid="browser-live-card"] .group')?.classList.add('capture-hover')
  const style = document.createElement('style')
  style.textContent = '.capture-hover > [aria-hidden="true"].opacity-0 { opacity: 1 !important }'
  document.head.appendChild(style)
}
document.documentElement.setAttribute('data-capture-ready', '1')
