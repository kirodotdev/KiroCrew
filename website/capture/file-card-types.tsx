/**
 * Evidence sheet for the attachment card's per-type glyph: the REAL FileCard,
 * one row per file family, so the frame shows the header grid and the chosen
 * icon exactly as the transcript renders them. Captions are inline-styled:
 * `capture/` is outside the Tailwind content glob (see transcript-row-style.tsx).
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n'
import { FileCard, type FileData } from '../src/components/FileCard'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const FILES: FileData[] = [
  { filename: 'q3-report.pdf', content_type: 'application/pdf', size: 248320, description: 'Q3 cost breakdown' },
  { filename: 'headcount.xlsx', content_type: 'application/octet-stream', size: 91234 },
  { filename: 'kickoff-deck.pptx', size: 4210000, description: 'Slides for Monday' },
  { filename: 'dashboard.png', content_type: 'image/png', size: 284000, description: 'Dashboard preview' },
  { filename: 'demo.mp4', content_type: 'video/mp4', size: 8200000, description: 'Feature walkthrough' },
  { filename: 'voice-note.mp3', content_type: 'audio/mpeg', size: 940000, description: 'Meeting note' },
  { filename: 'release-notes.md', content_type: 'text/markdown; charset=utf-8', size: 3120 },
  { filename: 'bundle.tsx', size: 18400 },
  { filename: 'config.yaml', content_type: 'application/yaml', size: 880 },
  { filename: 'gateway.log', size: 1048576 },
  { filename: 'site-backup.tar.gz', content_type: 'application/gzip', size: 73400320 },
  { filename: 'deploy-key.pem', content_type: 'application/octet-stream', size: 1704 },
  { filename: 'KiroCrew-1.4.0.dmg', size: 182000000 },
  { filename: 'cache.sqlite', size: 5242880 },
  { filename: 'fix-race.patch', size: 2210 },
  { filename: 'Inter.woff2', content_type: 'font/woff2', size: 98000 },
  { filename: '.env', size: 210 },
  { filename: 'analysis.ipynb', size: 44100 },
  { filename: 'bracket.stl', content_type: 'model/stl', size: 1200000 },
  { filename: 'handbook.epub', content_type: 'application/epub+zip', size: 2400000 },
  { filename: 'standup.ics', content_type: 'text/calendar', size: 1200 },
  { filename: 'blob', size: 512 },
]

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <div data-capture-root className="bg-bg text-text" style={{ width: 700, padding: '12px 16px 16px', display: 'grid', gap: 8 }}>
    {FILES.map(f => (
      <div key={f.filename} data-block={f.filename}>
        <FileCard file={f} />
      </div>
    ))}
  </div>,
)
