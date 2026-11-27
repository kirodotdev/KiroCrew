/**
 * Evidence: a registered theme's overlay on the LOCAL dashboard is not drawn
 * over a remote Crew's pane.
 *
 * The page stands in for the desktop window while a remote Crew tab is shown:
 * a full-viewport remote pane (its own colors, its own composer) with the local
 * dashboard's registered-theme mascot (the core ghost art standing in for the
 * theme's character) pinned bottom-right at z-index 40.
 *
 *   ?arm=before   main's mount: `overlaysOn && branding.overlays.map(...)` at the
 *                 app root, with no remote-Crew gate (copied from App.tsx on main)
 *   ?arm=after    the real `RegisteredThemeOverlays` from this change
 *   ?crew=remote  a remote Crew is shown (instances.activeId = 'cd-1', default)
 *   ?crew=local   the local dashboard is shown (activeId = null)
 *
 * Component render, not the live page.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'

import RegisteredThemeOverlays from '../src/shell/RegisteredThemeOverlays'
import { GhostVar1 } from '../src/assets/onboarding/GhostIcons'
import { createTestStore } from '../src/test/helpers'
import { setActiveId } from '../src/store/instancesSlice'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const arm = params.get('arm') === 'before' ? 'before' : 'after'
const remote = params.get('crew') !== 'local'
document.documentElement.dataset.mode = 'dark'
document.documentElement.dataset.theme = 'kiro-dark'
await initI18n()

const store = createTestStore()
if (remote) store.dispatch(setActiveId('cd-1'))

function Mascot() {
  return (
    <div data-capture-mascot style={{ position: 'fixed', bottom: -30, right: 40, zIndex: 40, pointerEvents: 'none' }}>
      <GhostVar1 width={170} />
    </div>
  )
}
const overlays = [Mascot]

function Pane() {
  return (
    <div style={{ position: 'fixed', inset: 0, display: 'flex', flexDirection: 'column', background: remote ? '#0f2a24' : 'var(--bg)', color: 'var(--text)', zIndex: 1 }}>
      <div style={{ padding: '10px 16px', borderBottom: '1px solid var(--border)', fontSize: 13 }}>
        {remote ? 'Cloud Desktop · remote Crew pane (stand-in)' : 'Local dashboard'}
      </div>
      <div style={{ flex: 1, padding: 24, fontSize: 15, lineHeight: 1.6 }}>
        {remote
          ? 'The remote Crew uses its own theme. Nothing from the local theme should be drawn here.'
          : 'The local dashboard uses a theme with a corner character.'}
      </div>
      <div style={{ margin: '0 24px 20px', padding: '14px 16px', border: '1px solid var(--border)', borderRadius: 16, background: 'var(--bg-elevated)' }}>
        Give me the CLI command for the integ endpoint, the end of this line must stay readable
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <Pane />
    {arm === 'before'
      ? overlays.map((Overlay, i) => <Overlay key={i} />)
      : <RegisteredThemeOverlays colorTheme="capture-theme" overlays={overlays} slot={null} />}
  </Provider>,
)
