/**
 * Evidence harness for folding a turn's reasoning bursts into ONE row.
 *
 * WHY ISOLATED: the wall only appears in a long turn that reasons, calls a
 * tool, reasons again — many times over — and then settles (a stopped
 * prepare-pr round, a monitor cycle). That shape cannot be provoked on demand
 * in a live session, and its reasoning is never persisted (the backend
 * broadcasts `chat_thinking` and drops it), so it cannot be replayed from
 * history either. The sibling `thinking-bursts` harness documents the same
 * constraint.
 *
 * WHAT IS FAITHFUL: unlike `thinking-bursts` (which renders rows FLAT to
 * photograph the per-burst reducer output), THIS harness renders the turn
 * through the real ChatPage collapse container — `TurnBlock` in the default
 * "show thinking inline" mode (`collapseAll=false`) — because the fold under
 * test lives in TurnBlock, not in a component. Nothing about TurnBlock or
 * ThinkingBlock is mocked; only the row-renderer switch is local and renders
 * the same components ChatPage does.
 *
 * Run the SAME page against the pre-change TurnBlock (git stash) for the
 * "before" frame and the patched one for "after"; the harness itself never
 * changes, so the delta it shows is only the code.
 *
 *   ?theme=dark|light &bursts=6
 */
import { useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import { RowDisclosureProvider } from '../src/pages/chat/rowDisclosure'
import ThinkingBlock from '../src/pages/chat/ThinkingBlock'
import TurnBlock from '../src/pages/chat/TurnBlock'
import type { DisplayItem, TurnItem } from '../src/pages/chat/types'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const burstCount = Math.max(2, Number(params.get('bursts') || 6))
// `?stream=1`: render a RUNNING turn (complete=false) whose single folded row
// keeps growing, so the recording shows the live "Thinking" shimmer + tail.
const stream = params.get('stream') === '1'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** A distinct, sentence-shaped reasoning body per burst, so a stack of them is
 *  visibly a stack (not one repeated line) and the merged form visibly carries
 *  every burst's text. */
const BURSTS = [
  'The user is asking why so many "Thought process" rows appear. Let me check how the reasoning trace reaches the transcript before answering.',
  'Confirmed: chatSlice opens one thinking message per burst (#4178), one above each tool step. That is why a long turn stacks them.',
  'So the wall is the per-burst design meeting a completed turn — the interleaved tool calls fold away and only the reasoning rows remain.',
  'The fix should stay render-only: keep the per-burst messages in the store, but fold them into one row when the turn is drawn.',
  'Anchoring the merged row at the FIRST burst keeps the reasoning where it started, above the first tool, matching the old single-block look.',
  'Because the merged content grows as bursts arrive, ThinkingBlock’s existing liveness fires — one live line while working, one settled block after.',
]

const TS = '2026-08-24T23:30:00.000Z'

/** think → tool → think → tool … → answer, the shape that produced the wall. */
function buildTurn(n: number, complete: boolean, tail = ''): Extract<DisplayItem, { kind: 'turn' }> {
  const msgs: ChatMessage[] = []
  for (let i = 0; i < n; i++) {
    const body = BURSTS[i % BURSTS.length]
    // In stream mode the LAST burst keeps growing (the tail), which is what
    // drives ThinkingBlock's content-growth liveness on the merged row.
    const content = !complete && i === n - 1 ? body + tail : body
    msgs.push({ role: 'thinking', content, cls: '', ts: TS, meta: { clientTs: `think-${i}` } })
    msgs.push({ role: 'tool', content: '🔧 Running: read', cls: '', ts: TS, meta: { tool_call_id: `t${i}`, purpose: 'read a file' } })
  }
  if (complete) {
    msgs.push({ role: 'assistant', content: 'Root cause: the per-burst design meets a completed turn. Folding the bursts into one row fixes the wall.', cls: 'msg msg-a', ts: TS })
  }
  const items: TurnItem[] = msgs.map((msg, idx) => ({ kind: 'single', msg, idx }))
  return { kind: 'turn', items, complete }
}

const STREAM_WORDS = ' — checking the reducer, then the render path, then the fold anchor,'

function ToolPill() {
  return <div className="text-[12px] text-muted/70 leading-5">🔧 read</div>
}

function TurnFoldCapture() {
  // In stream mode the last burst grows on a timer; otherwise the turn is a
  // settled snapshot.
  const [tail, setTail] = useState('')
  useEffect(() => {
    if (!stream) return
    let i = 0
    const timer = setInterval(() => {
      setTail(t => t + STREAM_WORDS.split(' ')[i % STREAM_WORDS.split(' ').length] + ' ')
      i += 1
    }, 180)
    return () => clearInterval(timer)
  }, [])
  const turn = useMemo(() => buildTurn(stream ? 3 : burstCount, !stream, tail), [tail])
  const renderItem = (it: TurnItem, i: number) => {
    if (it.kind !== 'single') return null
    const m = it.msg
    return (
      <div key={`row-${i}`} className="px-5 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
        {m.role === 'thinking'
          ? <ThinkingBlock content={m.content} disclosureKey={`row-${it.idx}`} />
          : m.role === 'tool'
            ? <ToolPill />
            : <div className="text-[14px] text-text leading-6">{m.content}</div>}
      </div>
    )
  }
  return <TurnBlock turn={turn} renderItem={renderItem} collapseAll={false} />
}

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    className="bg-bg text-text"
    style={{ width: 900, minHeight: '100vh', paddingTop: 16, paddingBottom: 16, ['--mc-content-width' as string]: '760px' }}
  >
    <RowDisclosureProvider resetKey="capture">
      <TurnFoldCapture />
    </RowDisclosureProvider>
  </div>,
)
