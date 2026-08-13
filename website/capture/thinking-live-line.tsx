/**
 * Isolated capture entry for the collapsed reasoning row's live preview.
 *
 * WHY ISOLATED: a reasoning block only exists inside a streaming turn, which
 * needs the app shell, a live websocket and a seeded session; a half-stubbed
 * shell renders its error boundary instead, and a screenshot of the wrong thing
 * is worse evidence than none.
 *
 * What MUST be faithful is the row itself, so nothing about the component is
 * mocked: the real ThinkingBlock renders against the real stylesheet, and the
 * chunks are appended on a timer at the same seam the WS reducer writes — so the
 * preview's own liveness rule (content growing) is what drives the frame.
 *
 * Scene + theme come from the query string: ?scene=long&theme=dark
 */
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'

// Initialise i18next exactly as main.tsx does. Importing the module only DEFINES
// initI18n — without calling it, every label in the frame is blank, which
// silently produces screenshots that misrepresent the real UI.
import { initI18n } from '../src/i18n'
import { RowDisclosureProvider } from '../src/pages/chat/rowDisclosure'
import ThinkingBlock from '../src/pages/chat/ThinkingBlock'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'long'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SEED: Record<string, string> = {
  short: 'The user wants',
  long: 'The user wants the collapsed reasoning row to show what the model is thinking, one line is enough. Checking how chat_thinking already reaches the transcript',
  settled:
    'The user wants the collapsed reasoning row to show what the model is thinking.\nThe chunks already reach the transcript, so this is a frontend-only change.',
}

const CHUNKS = [
  ' — so the row keeps',
  ' the label and adds',
  ' the tail of the trace',
  ' on the same line.',
]

/** Appends chunks on a timer, the way `sseThinkingChunk` grows the message, then
 *  stops -- so a recording shows the preview settling back off as well as
 *  running. The `settled` scene never appends, which is what a finished block
 *  looks like on mount. */
function Stream({ seed, live, chunks }: { seed: string; live: boolean; chunks: number }) {
  const [content, setContent] = useState(seed)
  useEffect(() => {
    if (!live) return
    let i = 0
    const timer = setInterval(() => {
      if (i >= chunks) { clearInterval(timer); return }
      setContent(c => c + CHUNKS[i % CHUNKS.length])
      i += 1
    }, 120)
    return () => clearInterval(timer)
  }, [live, chunks])
  return <ThinkingBlock content={content} disclosureKey="capture-row" />
}

initI18n()

createRoot(document.getElementById('root')!).render(
  <div
    style={{
      background: 'var(--bg)',
      padding: 24,
      minHeight: '100vh',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'flex-start',
      gap: 12,
      fontFamily: 'var(--font-ui, system-ui)',
    }}
  >
    <RowDisclosureProvider resetKey="capture">
      <Stream
        seed={SEED[scene] || SEED.long}
        live={scene !== 'settled'}
        chunks={Number(params.get('chunks') || 1000)}
      />
    </RowDisclosureProvider>
  </div>,
)
