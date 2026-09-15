/**
 * Isolated capture entry for #7819: the chat selection toolbar on a reply that is
 * STILL STREAMING.
 *
 * WHY ISOLATED: reaching this state in the real app needs a gateway, an open slot
 * and a reply caught mid-flight — none of which a screenshot run can stage
 * deterministically. So this mounts the REAL `AssistantMessage` with
 * `isStreaming` set, which is the component that owns the gate under review.
 *
 * WHY NOT `externalSelection`: the sibling scene `selection-toolbar-ask` drives
 * the toolbar through that prop, which mounts it directly and would bypass the
 * very condition this change is about. Here the driver makes a real DOM range
 * inside the bubble and fires the `mouseup` the toolbar listens for, so what the
 * shot proves is that `AssistantMessage` now renders the toolbar while
 * `isStreaming` is true — not merely that the toolbar can be rendered.
 *
 * The passage is long enough that the selected span sits mid-paragraph, so the
 * shot also shows the streaming glow above the toolbar.
 *
 * Language + theme come from the query string: ?lang=zh-CN&theme=light
 */
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const lang = params.get('lang') || 'en'
const theme = params.get('theme') || 'dark'
// `?stream=1` grows the content word by word on a real timer, so the markdown
// subtree is genuinely re-parsed while a selection is held. Without it the scene
// is a single static frame, which is enough for a screenshot but CANNOT answer
// whether a selection survives token arrival.
const streaming = params.get('stream') === '1'
const intervalMs = Number(params.get('interval') || 80)

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const CONTENT = [
  'Reading the gateway logs now. The reconnect storm starts when the session',
  'lease expires while a turn is still in flight, so the client retries against',
  'a slot the server has already released.',
].join(' ')

// A paragraph that is COMPLETE before the stream starts, so the scene has a
// settled block to select in as well as a growing one. The blank line is what
// makes them separate markdown blocks.
const SETTLED = [
  'Here is what the trace shows. The lease renewal and the turn boundary are',
  'independent clocks, and nothing forces them to agree.',
].join(' ')

const TAIL_WORDS = CONTENT.split(' ')

function Harness() {
  // In stream mode the tail starts empty and fills in; otherwise it is whole.
  const [wordCount, setWordCount] = useState(streaming ? 0 : TAIL_WORDS.length)
  useEffect(() => {
    if (!streaming) return
    const id = setInterval(() => {
      setWordCount((n) => (n >= TAIL_WORDS.length ? n : n + 1))
    }, intervalMs)
    return () => clearInterval(id)
  }, [])

  const tail = TAIL_WORDS.slice(0, wordCount).join(' ')
  const content = streaming ? `${SETTLED}\n\n${tail}` : CONTENT

  return (
    <div
      data-testid="scene"
      data-words={wordCount}
      style={{ width: 660, margin: '36px auto 0', color: 'var(--text)', font: '14px/1.6 var(--font-body, sans-serif)' }}
    >
      <AssistantMessage
        content={content}
        isStreaming={true}
        slotRunning={true}
        onQuote={(text) => { (window as unknown as { __quoted?: string }).__quoted = text }}
        onAsk={() => {}}
      />
    </div>
  )
}

initI18n(lang)
createRoot(document.getElementById('root')!).render(<Harness />)
