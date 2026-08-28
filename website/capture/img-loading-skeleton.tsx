/**
 * Evidence for the transcript image loading skeleton.
 *
 * THE PROBLEM. A not-yet-loaded markdown image reserved only a min-HEIGHT.
 * An unloaded <img> has no intrinsic size and max-width is only a cap, so the
 * element collapsed to a 0-wide border sliver — a message carrying several
 * screenshots rendered as bare vertical lines while the bytes were in flight,
 * reading as missing content.
 *
 * THE SCENE. The REAL MarkdownRenderer (its classes live under src/, so
 * Tailwind compiles them — capture/ is outside the scan glob), driven into
 * each loading state by the capture script holding the image requests
 * pending: the fixed pending box (dimensions unknown), the compact
 * (sent-prompt) box, the learned exact reserve (dimensions seeded as a prior
 * load would), and the released loaded state for comparison.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { rememberImageDims } from '../src/utils/imageDims'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Seed the dims a prior successful load of this URL would have recorded, so
// the "learned" episode mounts with the exact-reserve skeleton.
rememberImageDims('https://cap.test/pending-learned.png', 640, 360)

const PENDING_MD =
  'Before — the divider ran to the top edge:\n\n' +
  '![before](https://cap.test/pending-a.png)\n\n' +
  'After — inset 12px at both ends:\n\n' +
  '![after](https://cap.test/pending-b.png)'

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <div data-episode={id} style={{ marginBottom: 24 }}>
      <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--muted)', margin: '0 0 6px' }}>
        {title}
      </div>
      {children}
    </div>
  )
}

function App() {
  return (
    <div
      data-capture-root=""
      style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24, maxWidth: 860 }}
    >
      <div data-capture-group="loading">
        <Section id="pending" title="LOADING · DIMENSIONS UNKNOWN — fixed 420x236 box, pulse + icon">
          <MarkdownRenderer content={PENDING_MD} />
        </Section>
        <Section id="pending-compact" title="LOADING · SENT-PROMPT THUMBNAIL — fixed 240x180, end-aligned">
          <MarkdownRenderer content={'![shot](https://cap.test/pending-c.png)'} compactImages />
        </Section>
        <Section id="learned" title="LOADING · DIMENSIONS LEARNED — exact 640x360 reserve">
          <MarkdownRenderer content={'![shot](https://cap.test/pending-learned.png)'} />
        </Section>
      </div>
      <div data-capture-group="loaded">
        <Section id="loaded" title="LOADED — skeleton released, natural layout">
          <MarkdownRenderer content={'![shot](https://cap.test/loaded.png)'} />
        </Section>
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
