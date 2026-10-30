/**
 * Evidence page for a file_send image card opening the image viewer: one turn
 * as the transcript draws it, a reply with a markdown image followed by the
 * REAL FileCard, inside a `data-image-turn` wrapper (the transcript's
 * per-display-item wrapper), with the real Lightbox mounted. The shoot script
 * answers /api/outbox and /api/file-raw with fixture PNGs.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n'
import { FileCard } from '../src/components/FileCard'
import MarkdownRenderer, { Lightbox } from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <div className="bg-bg text-text min-h-screen">
    <div data-capture-root data-image-turn="" style={{ width: 760, padding: '16px 20px 20px', display: 'grid', gap: 10 }}>
      <MarkdownRenderer content={'Here is the pipeline board before the fix:\n\n![Pipeline board, light theme](/tmp/board-before.png)\n\nAnd the dark-theme render after it, sent as a file:'} />
      <FileCard file={{ filename: 'pipeline-board-dark.png', content_type: 'image/png', size: 284113, description: 'Pipeline board after the fix' }} />
    </div>
    <Lightbox />
  </div>,
)
