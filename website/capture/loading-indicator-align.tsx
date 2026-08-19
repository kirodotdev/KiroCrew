/**
 * Visual evidence for "the running indicator starts on the transcript's own
 * left edge".
 *
 * WHY ISOLATED: the indicator only exists while a turn is in flight, and it
 * hides itself again the moment text starts arriving (MarkdownRenderer owns the
 * caret there). Photographing it in a live session means catching a tool-call
 * gap, which is not reproducible on demand.
 *
 * WHAT IS FAITHFUL is the COLUMN GEOMETRY, since that is the whole claim. The
 * `after` scene renders the REAL ChatFooter, which supplies its own row wrapper
 * because ChatPage renders it raw, as a sibling of the row wrappers rather than
 * inside one. The `before` scene is a literal replica of the pre-fix markup:
 * the same wrapper plus the `px-3.5` inner div it used to carry, around the same
 * real SwapCarousel.
 *
 * The reference rows use the literal host wrapper every transcript row gets
 * (`px-4 mx-auto w-full py-1` under `--mc-content-width`, ChatPage.tsx:6625).
 * No row component re-applies that gutter — TranscriptRowGeometry.test.tsx pins
 * it — so a row's content box always starts on the wrapper's padding edge, which
 * is the dashed line.
 *
 *   ?scene=before|after &theme=dark|light
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6816 --strictPort
 *   node scripts/capture-loading-indicator-align.mjs http://127.0.0.1:6816 \
 *     ../temp-screenshots/loading-indicator-align
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { Wrench } from 'lucide-react'

import { initI18n } from '../src/i18n'
import ChatFooter, { SwapCarousel, resolveLoaderIcons } from '../src/pages/chat/ChatFooter'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'before' ? 'before' : 'after'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const COLUMN = { maxWidth: 'var(--mc-content-width, 900px)' } as const

/** A transcript row in the literal wrapper the host puts around every message. */
function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div data-row={label} className="px-4 mx-auto w-full py-1" style={COLUMN}>
      {children}
    </div>
  )
}

/**
 * The pre-fix footer, reproduced exactly: the row wrapper it still has, plus the
 * `px-3.5` inner div that stacked 14px on top of the wrapper's own gutter.
 */
function FooterBefore() {
  return (
    <div data-testid="chat-footer" className="px-4 mx-auto w-full py-1" style={COLUMN}>
      <div className="px-3.5 py-2.5">
        <SwapCarousel icons={resolveLoaderIcons('kiro')} />
      </div>
    </div>
  )
}

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <div
      data-capture-root
      data-scene={scene}
      className="bg-bg text-text relative"
      style={{ width: 900, ['--mc-content-width' as string]: '800px' }}
    >
      {/* The column's text edge: a 50px page gutter plus the 16px of px-4. */}
      <div
        className="absolute top-0 bottom-0 border-l border-dashed border-accent/40 pointer-events-none"
        style={{ left: 'calc(50% - 400px + 16px)' }}
      />
      <div className="py-4">
        <Row label="assistant text">
          <div className="text-[14px] leading-6">Rebasing the branch onto main now.</div>
        </Row>
        <Row label="tool call">
          <div className="flex items-center gap-1.5 text-[13px] text-muted">
            <Wrench className="lucide-inline" size={10} /> fs_read
          </div>
        </Row>
        {scene === 'before' ? <FooterBefore /> : (
          <ChatFooter running stopping={false} state="running" lastRole="assistant" />
        )}
      </div>
    </div>
  </MemoryRouter>,
)
