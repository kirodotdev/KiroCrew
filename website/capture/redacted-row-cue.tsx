/**
 * Isolated capture entry for the rewritten-row cue on a persisted user message.
 *
 * Mounts the REAL `UserMessage` fed by the REAL `MarkdownRenderer`, so the still
 * shows the shipped footer chain rather than a mock of it. The gate below fails
 * the capture if the cue renders the raw i18n key, so a green screenshot cannot
 * be a picture of a broken surface.
 *
 * Two rows, because the question is a COMPARISON -- whether a reader can tell a
 * rewritten row from a verbatim one. The first carries the stored
 * `[REDACTED: credential]` tag and the cue; the second is ordinary.
 *
 * Theme: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { i18nT } from '../src/i18n/t'
import UserMessage from '../src/pages/chat/UserMessage'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'kiro-light' : 'kiro-dark'
document.documentElement.setAttribute('data-theme', theme)

initI18n('en')

/** The stored body a persister writes once the scrub rewrites a span: the tag is
 *  what lands on disk and is served back, and no original is kept. */
const REWRITTEN = 'deploy with token [REDACTED: credential] and retry the failed shard'
/** Same shape of request, nothing scrubbed -- the contrast row. */
const VERBATIM = 'deploy with the staging profile and retry the failed shard'
/** A host whose companion policy fails to compose withholds the whole body. The
 *  backend counts this placeholder as a rewrite too, so the row is cued. */
const WITHHELD = '<withheld: redaction unavailable>'

const render = (c: string) => <MarkdownRenderer content={c} softBreaks />

/** The literal per-row transcript wrapper and user-row chain. */
function Row({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className="group flex flex-col min-w-0 items-end">
        <div className="flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full items-end">
          {children}
        </div>
      </div>
    </div>
  )
}

function Scene() {
  return (
    <div className="bg-bg text-text min-h-screen py-6" data-capture-root>
      <Row>
        <UserMessage
          content={REWRITTEN}
          redacted
          messageTs="row-rewritten"
          timestamp="3:41 PM"
          timestampTitle="Wednesday, September 16, 2026 at 3:41 PM"
          renderContent={render}
        />
      </Row>
      <Row>
        <UserMessage
          content={VERBATIM}
          messageTs="row-verbatim"
          timestamp="3:42 PM"
          timestampTitle="Wednesday, September 16, 2026 at 3:42 PM"
          renderContent={render}
        />
      </Row>
      <Row>
        <UserMessage
          content={WITHHELD}
          redacted
          messageTs="row-withheld"
          timestamp="3:43 PM"
          timestampTitle="Wednesday, September 16, 2026 at 3:43 PM"
          renderContent={render}
        />
      </Row>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<MemoryRouter><Scene /></MemoryRouter>)

declare global {
  interface Window {
    __captureGate: () => { cueText: string; cueCount: number }
  }
}

window.__captureGate = () => {
  const cues = document.querySelectorAll('[data-testid="user-message-redacted"]')
  const cueText = (cues[0]?.textContent || '').trim()
  const expected = i18nT('pages.chat.userMessage.row_redacted')
  if (cues.length !== 2) {
    throw new Error(`capture gate: expected two cues, saw ${cues.length}`)
  }
  if (cueText !== expected || cueText.includes('userMessage.')) {
    throw new Error(`capture gate: cue reads ${JSON.stringify(cueText)}, expected ${JSON.stringify(expected)}`)
  }
  if (!document.body.textContent?.includes(WITHHELD)) {
    throw new Error('capture gate: the withheld row body is not on the page')
  }
  return { cueText, cueCount: cues.length }
}
