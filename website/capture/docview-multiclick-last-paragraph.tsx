/**
 * Isolated capture entry for the spec-builder document pane's last-paragraph
 * selection (#7891).
 *
 * Like `selection-toolbar-multiclick.tsx` (#7847) this mounts the real component
 * and lets the driving script perform a genuine Chromium triple-click, because
 * the defect lives in the REAL selection path: Chromium normalizes a
 * triple-click of the pane's last paragraph to a boundary point past the pane,
 * and `onSelectionSettled`'s containment check then cleared the selection so the
 * Comment pill never appeared. No selection is injected — the browser makes its
 * own, which is the only way the boundary geometry is real.
 *
 * The document mirrors the report: several paragraphs whose LAST one is the
 * triple-click target, with content after the pane so a normalized selection end
 * has somewhere outside to land.
 *
 * Language + theme come from the query string: ?lang=zh-CN&theme=light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import DocView from '../src/apps/spec-builder/components/DocView'
import type { SpecDetail } from '../src/apps/spec-builder/api'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const lang = params.get('lang') || 'en'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const DOC = [
  'The gateway separates where the agent runs from where you work with it.',
  '',
  'A session is bound to one workspace, so its files and memory travel together.',
  '',
  'Triple-click this final paragraph to raise the Comment pill.',
].join('\n')

const detail = {
  name: 'selection-containment',
  working_dir: '/w',
  spec_dir: '/w/.kiro/specs/selection-containment',
  spec_type: 'feature',
  status: 'planning',
  phase: 'design',
  running: false,
  files: { 'design.md': DOC },
  state: null,
  context: {},
} as unknown as SpecDetail

function Harness() {
  return (
    <div style={{ width: 720, margin: '60px auto 0', color: 'var(--text)' }}>
      <div style={{ height: 190, display: 'flex', flexDirection: 'column' }}>
        <DocView detail={detail} tab="design" addComment={() => {}} />
      </div>
      {/* Content AFTER the scroll pane is load-bearing, not decoration: Chromium
          normalizes a triple-click of the last paragraph to "the start of the
          next block", so without a following block the selection end has nowhere
          outside the pane to land, the common ancestor stays inside it, and the
          pre-fix component passes — the capture would be a portrait rather than
          a regression proof. In the real page the pane is followed by the
          comment tray and the composer footer. */}
      <p data-testid="after-pane" style={{ opacity: 0.5, marginTop: 20, font: '13px/1.6 var(--font-body, sans-serif)' }}>
        The comment tray sits below the document pane.
      </p>
    </div>
  )
}

initI18n(lang)
createRoot(document.getElementById('root')!).render(<Harness />)
