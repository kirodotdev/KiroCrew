/**
 * Isolated capture entry for the update progress overlay.
 *
 * WHY ISOLATED: the overlay only mounts mid-update (App gates it on
 * `updating || showUpdateModal`), a state a full-shell capture cannot reach
 * without stubbing the update endpoints end to end — and a half-stubbed shell
 * renders its error boundary, which is worse evidence than none.
 *
 * This mounts the exported UpdateOverlay against the REAL stylesheet and the
 * REAL redux store, seeding `updateProgress` and the connection flag through
 * the same reducers the WS frames use in production.
 *
 * Scenes (?scene=...&theme=dark):
 *   restarting   — the gateway pushed the `restarting` step, socket still up:
 *                  the idle "page will reconnect when ready" copy.
 *   reconnecting — the socket dropped mid-restart (the gateway exec'd itself):
 *                  the explicit "Gateway is restarting — reconnecting…" state
 *                  this PR adds, in place of the frozen step list.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'

// Initialise i18next exactly as main.tsx does — without this every label in
// the captured frame renders blank (see capture/update-card.tsx).
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { setUpdateProgress, sseConnected, sseDisconnected } from '../src/store/dashboardSlice'
import { UpdateOverlay } from '../src/App'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'restarting'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

store.dispatch(setUpdateProgress({ step: 'restarting', detail: 'Restarting server…' }))
store.dispatch(scene === 'reconnecting' ? sseDisconnected() : sseConnected())

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <UpdateOverlay onCancel={() => {}} />
  </Provider>,
)
