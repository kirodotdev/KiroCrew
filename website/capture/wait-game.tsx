/**
 * Isolated capture entry for the laundry game on the dashboard's wait surfaces.
 *
 * WHY ISOLATED: both surfaces exist only while the gateway is unreachable (the
 * host-offline cover) or restarting mid-update (the update overlay), states a
 * full-shell capture cannot reach without a dead gateway.
 *
 * Mounts the real components against the real stylesheet and store. The game
 * is started the way a person starts it early: a Space keypress on the page.
 *
 * Scenes (?scene=...&theme=dark|light):
 *   host   — the host-offline cover (`RelayReconnectingView cover="host"`).
 *   update — the update overlay with its socket dropped mid-restart.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { setUpdateProgress, sseDisconnected } from '../src/store/dashboardSlice'
import { UpdateOverlay } from '../src/App'
import { RelayReconnectingView } from '../src/shell/RelayReconnectingView'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'host'
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')
store.dispatch(sseDisconnected())
if (scene === 'update') store.dispatch(setUpdateProgress({ step: 'restarting', detail: 'Restarting server…' }))

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    {scene === 'update'
      ? <UpdateOverlay onCancel={() => {}} />
      : <div className="relative h-screen w-screen bg-bg"><RelayReconnectingView cover="host" /></div>}
  </Provider>,
)
