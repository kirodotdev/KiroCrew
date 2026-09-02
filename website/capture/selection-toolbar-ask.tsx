/**
 * Isolated capture entry for the chat selection toolbar's action row.
 *
 * WHY ISOLATED: the toolbar only mounts on a real text selection inside a live
 * chat transcript, which needs a gateway and an open slot. This mounts the REAL
 * `SelectionToolbar` with the REAL `useSelectionActions` row and drives it
 * through the component's own `externalSelection` prop — the same escape hatch
 * Monaco uses — so the buttons, their icons and their translated labels are the
 * shipped ones rather than a posed copy.
 *
 * The row's width is the thing under review: "Ask in Side Chat" names the panel
 * the question lands in, which is longer than its neighbours, so the shot has to
 * show all three buttons together at their real type size.
 *
 * Language + theme come from the query string: ?lang=zh-CN&theme=light
 */
import { useRef } from 'react'
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import SelectionToolbar, { useSelectionActions } from '../src/components/SelectionToolbar'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const lang = params.get('lang') || 'en'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

function Harness() {
  const containerRef = useRef<HTMLDivElement>(null)
  const actions = useSelectionActions(
    () => {},
    () => {},
  )
  return (
    <div
      ref={containerRef}
      style={{ width: 640, margin: '120px auto 0', color: 'var(--text)', font: '14px/1.6 var(--font-body, sans-serif)' }}
    >
      <p data-testid="passage">
        The gateway separates where the agent runs from where you work with it.
      </p>
      {/* Position the row under the passage rather than over it, so the shot is
          the buttons themselves and not a toolbar overlapping its own context. */}
      <SelectionToolbar
        containerRef={containerRef}
        actions={actions}
        externalSelection={{ text: 'where the agent runs', x: 320, y: 220 }}
      />
    </div>
  )
}

initI18n(lang)
createRoot(document.getElementById('root')!).render(<Harness />)
