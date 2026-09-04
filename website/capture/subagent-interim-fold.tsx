/**
 * Evidence harness for folding a sub-agent fan-out's interim turns.
 *
 * WHY ISOLATED: the shape only occurs when a wave of sub-agents lands one
 * result at a time and the agent replies to each before the synthesis turn
 * fires. That cannot be provoked on demand in a live session (it needs several
 * real sub-agents to finish minutes apart), and the interim replies are
 * indistinguishable from ordinary answers in history, so it cannot be replayed
 * from a transcript either. The sibling `thinking-turn-fold` harness documents
 * the same constraint.
 *
 * WHAT IS FAITHFUL: the message list below is the ROW SEQUENCE OF A REAL
 * SESSION (dashboard_chat-275) and it is fed through the REAL pipeline —
 * `groupDisplayItems` -> `applyRunningState` -> `TurnBlock` — because the change
 * under test spans both the grouping pass (which marks the region) and TurnBlock
 * (which folds it). Neither is mocked; only the row-renderer switch is local,
 * and it renders the same components ChatPage does.
 *
 * BEFORE/AFTER: `?fold=0` strips the `interim` flag from the grouped output,
 * which reproduces the pre-change rendering exactly — the old code produced no
 * such flag, and its `synthesisPending` suppression never fired in practice
 * (no persisted row in any inspected session carried that meta). So the two
 * frames differ only in whether the fold is applied, with one harness.
 *
 *   ?theme=dark|light &fold=0|1
 */
import { useMemo } from 'react'
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import { RowDisclosureProvider } from '../src/pages/chat/rowDisclosure'
import TurnBlock from '../src/pages/chat/TurnBlock'
import { groupDisplayItems, applyRunningState } from '../src/pages/chat/groupDisplayItems'
import type { DisplayItem, TurnItem } from '../src/pages/chat/types'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const fold = params.get('fold') !== '0'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const TS = '2026-08-30T21:50:00.000Z'
const row = (role: string, content: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: '', ts: TS, meta } as unknown as ChatMessage)

/**
 * The real sequence: prompt -> spawn -> two per-completion summaries as results
 * land -> the synthesis injection -> the answer. Note there is no `subagent`
 * row: a completion delivered while the slot is idle is injected straight into a
 * turn without one, which is why the fold anchors on the synthesis row.
 */
const MESSAGES: ChatMessage[] = [
  row('user', 'detect the root cause then raise the suggestion'),
  row('assistant', 'Delegating three independent investigations.'),
  row('tool', '🔧 Running: @kirocrew-core/spawn_run', { tool_call_id: 's1', purpose: 'spawn three agents' }),
  row('assistant', 'Spawned 3 agents — code path and existing guards, the content that triggered it, and upstream prior art. Waiting for results.'),
  row('tool', '🔧 Reading result.txt:1', { tool_call_id: 't1', purpose: 'read a result' }),
  row('assistant', 'Root cause identified — and it overturns the long-line hypothesis I gave earlier. Investigation B is still running; A and C are in. The trigger is catastrophic regex backtracking in a grammar pattern, not an oversized input.'),
  row('tool', '🔧 Reading result.txt:1', { tool_call_id: 't2', purpose: 'read a result' }),
  row('assistant', 'All three in. The three investigations agree on the mechanism and disagree on the trigger, and the disagreement is resolvable. Correcting what I told you earlier: the oversized-line theory is dead.'),
  row('inject', '[SYSTEM] Sub-agent synthesis: all sub-agents you spawned have finished.', { injectKind: 'synthesis' }),
  row('assistant', '# Renderer crash: root cause and what to do\n\nPierre runs the tokenizer with no time limit and no cancellation, on a regex engine that can backtrack exponentially.'),
]

/** Strip the fold marker to reproduce the pre-change rendering (see BEFORE/AFTER). */
const unfold = (items: DisplayItem[]): DisplayItem[] =>
  items.map(d => (d.kind === 'turn' && d.interim ? { ...d, interim: false } : d))

function Row({ msg }: { msg: ChatMessage }) {
  if (msg.role === 'tool') return <div className="text-[12px] text-muted/70 leading-5">{msg.content}</div>
  if (msg.role === 'inject') {
    return (
      <div className="text-[12px] text-muted leading-5 border-l-2 border-[var(--border)] pl-2">
        {msg.content}
      </div>
    )
  }
  if (msg.role === 'user') {
    return <div className="text-[14px] text-text leading-6 font-medium">{msg.content}</div>
  }
  return <div className="text-[14px] text-text leading-6 whitespace-pre-wrap">{msg.content}</div>
}

const renderItem = (it: TurnItem, i: number) => {
  if (it.kind !== 'single') return null
  return (
    <div key={`row-${i}`} className="px-5 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <Row msg={it.msg} />
    </div>
  )
}

function InterimFoldCapture() {
  const items = useMemo(() => {
    const grouped = applyRunningState(groupDisplayItems(MESSAGES), false)
    return fold ? grouped : unfold(grouped)
  }, [])
  return (
    <>
      {items.map((d, i) =>
        d.kind === 'turn'
          ? <TurnBlock key={`t-${i}`} turn={d} renderItem={renderItem} collapseAll={false} />
          : <div key={`s-${i}`}>{renderItem(d, i)}</div>,
      )}
    </>
  )
}

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    className="bg-bg text-text"
    style={{ width: 900, minHeight: '100vh', paddingTop: 16, paddingBottom: 16, ['--mc-content-width' as string]: '760px' }}
  >
    <RowDisclosureProvider resetKey="capture">
      <InterimFoldCapture />
    </RowDisclosureProvider>
  </div>,
)
