/**
 * Isolated capture entry for the App SDK's `BrowserView` with its action list.
 *
 * The view's state comes from `browser_live` events and binary frames; both are
 * fed straight into the live-browser store, with a frame drawn in-browser, so the
 * scene needs no gateway and no Chrome. The view, the store, the canvas painter,
 * the stylesheet and the theme tokens are the shipped ones.
 *
 * Scenes: ?scene=open | folded | waiting
 * Theme:  &theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'

import { initI18n } from '../src/i18n'
import { BrowserView } from '../src/app-sdk/index'
import { AppIdentityProvider } from '../src/app-sdk/identity'
import { AppScopedApiProvider } from '../src/app-sdk/scopedApi'
import { browserLiveStore } from '../src/hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../src/hooks/useBrowserLive.frame'
import dashboardReducer from '../src/store/dashboardSlice'
import chatReducer from '../src/store/chatSlice'
import { PREVIEW_APP_BROWSER_SDK, cachePreviewFlag } from '../src/utils/previewFlags'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'open'
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

const SLOT = 'music-1'
cachePreviewFlag(PREVIEW_APP_BROWSER_SDK, true)
browserLiveStore.__setWritesForTests({
  ack: async () => ({}), visibility: async () => ({}), claim: async () => ({}), release: async () => ({}),
})

/** A 1280x720 "video page": a dark player with a title bar. */
async function mockFrame(): Promise<DecodedBrowserFrame> {
  const c = document.createElement('canvas')
  c.width = 1280
  c.height = 720
  const g = c.getContext('2d')!
  g.fillStyle = '#0f0f0f'
  g.fillRect(0, 0, 1280, 720)
  g.fillStyle = '#272727'
  g.fillRect(0, 0, 1280, 56)
  g.fillStyle = '#3d6ea8'
  g.fillRect(140, 90, 1000, 520)
  g.fillStyle = '#ffffff'
  g.beginPath(); g.moveTo(600, 290); g.lineTo(600, 410); g.lineTo(700, 350); g.closePath(); g.fill()
  g.fillStyle = '#ff0033'
  g.fillRect(140, 600, 420, 6)
  const blob: Blob = await new Promise(r => c.toBlob(b => r(b!), 'image/jpeg', 0.7))
  return {
    header: { type: 'browser_frame', session_key: SLOT, seq: 1, w: 1280, h: 720, mask: [] },
    jpeg: new Uint8Array(await blob.arrayBuffer()),
    receivedAt: 1000,
  }
}

const LABELS = ['Open youtube.com', 'Search "Travel is Meaningful"', 'Click the first result', 'Play the video', 'Read the player state', 'Add the next song to the queue', 'Check the queue']
const steps = LABELS.map((label, i) => ({ label, status: i === LABELS.length - 1 ? 'active' : 'done' }))
browserLiveStore.applyEvent({
  session_key: SLOT, turn_id: 'turn-1', state: 'live', url: 'https://www.youtube.com/watch?v=x', title: 'Cheer Chen - Travel is Meaningful - YouTube',
  browser: 'user_chrome', headless: false, steps, step: { index: 6, label: 'Check the queue' },
})
if (scene !== 'waiting') browserLiveStore.applyFrame(await mockFrame())

const dashboard = dashboardReducer(undefined, { type: '@@capture/init' })
const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer },
  preloadedState: {
    dashboard: { ...dashboard, slots: [{ key: SLOT, title: SLOT, messages: 1, running: true, app: 'music' }] as unknown as typeof dashboard.slots },
  },
})

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <AppIdentityProvider appId="music" origin="external">
      <AppScopedApiProvider allowedApiPaths={[]} navigateFn={() => {}} approvedBrowserVerbs={['view']} active>
        <div data-capture-root style={{ padding: 24, width: 760, background: 'var(--bg)' }}>
          <div className="rounded-xl border border-border bg-card p-2">
            <BrowserView slot={SLOT} aria-label="The Bot's page" />
          </div>
        </div>
      </AppScopedApiProvider>
    </AppIdentityProvider>
  </Provider>,
)

if (scene === 'folded') {
  await new Promise(r => setTimeout(r, 200))
  ;(document.querySelector('[data-testid="app-browser-view-actions-toggle"]') as HTMLButtonElement | null)?.click()
}
await new Promise(r => setTimeout(r, 300))
document.body.dataset.ready = '1'
