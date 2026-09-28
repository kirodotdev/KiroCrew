/**
 * Isolated capture entry for the multi-click selection fix (#7847).
 *
 * Unlike `selection-toolbar-ask.tsx` this deliberately does NOT use the
 * `externalSelection` escape hatch: the defect under review lives in the REAL
 * selection path (`checkSelection`'s containment logic), so the harness mounts
 * the toolbar against a real container and lets the driving script perform a
 * genuine Chromium double-click on the last line. The browser then produces its
 * own boundary-normalized selection — the exact geometry that used to dismiss
 * the toolbar — and the shot proves the toolbar now appears over it.
 *
 * The passage mirrors the report: a multi-paragraph assistant message whose
 * LAST line holds the double-clicked word, with sibling content after the
 * container so a normalized selection end has somewhere outside to land.
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
    <div style={{ width: 640, margin: '80px auto 0', color: 'var(--text)', font: '14px/1.6 var(--font-body, sans-serif)' }}>
      <div ref={containerRef} data-testid="bubble">
        <p>The gateway separates where the agent runs from where you work with it.</p>
        <p data-testid="last-line">Double-click a word on this final line to select it.</p>
      </div>
      <p style={{ opacity: 0.5, marginTop: 24 }}>A following message keeps the bubble from being the last node.</p>
      <SelectionToolbar containerRef={containerRef} actions={actions} />
    </div>
  )
}

initI18n(lang)
createRoot(document.getElementById('root')!).render(<Harness />)
