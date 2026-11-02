/**
 * Evidence for the scene popover's approval-failure notice.
 *
 * Mounts the REAL useSceneInteraction hook (its thread popover and pending
 * approval bar) against the real store, stylesheet, theme tokens and i18n
 * catalog, with one slot parked on a tool approval. The capture script answers
 * the decide the way the server does for an approval that is gone (a 404), so
 * the popover shows the ErrorNotice it renders for that refusal. Nothing here
 * re-implements the popover, its classes or its strings.
 *
 *   ?theme=dark|light
 */
import { useRef } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { useSceneInteraction, type SceneAgent } from '../src/hooks/useSceneInteraction'
import type { AgentSource } from '../src/hooks/useAgentSync'
import { store } from '../src/store'
import { initI18n } from '../src/i18n'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

initI18n('en')

const W = 640
const H = 420
const AGENTS: SceneAgent[] = [
  { id: 'slot-a', name: 'Deploy', x: 120, y: 120, running: true, detail: '4 msgs', kind: 'slot', color: '#8cf' },
]
const SOURCES: AgentSource[] = [{
  id: 'slot-a', name: 'Deploy', label: 'default', kind: 'slot', running: true, detail: '4 msgs',
  pendingApproval: {
    tool: 'execute_bash', requestId: 'req-9',
    target: { origin: 'native', id: 'req-9', slot: 'slot-a', mid: 'mid-9' },
  },
}]

function Scene() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const agentsRef = useRef<SceneAgent[]>(AGENTS)
  const { canvasProps, tooltipEl } = useSceneInteraction(
    canvasRef, agentsRef, W, H, { active: 'Working', idle: 'Idle' }, 10, undefined, SOURCES,
  )
  return (
    <div data-capture-root className="bg-bg text-text" style={{ position: 'relative', width: W, height: H }}>
      <canvas data-testid="scene" ref={canvasRef} width={W} height={H} {...canvasProps} />
      {tooltipEl}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <MemoryRouter>
      <Scene />
    </MemoryRouter>
  </Provider>,
)
