/**
 * Isolated capture + measurement entry for the message action row's FIRST
 * cell: its hover paint must sit entirely inside the transcript's
 * `overflow-hidden` message column, with both start corners visible.
 *
 * WHY ISOLATED: the defect is a 7px slice of a hover background, which needs
 * real layout plus a real pointer hover; happy-dom computes neither, and the
 * whole chat page would need a gateway and an open slot that are no part of
 * it. What IS part of it is the box chain between the content column and the
 * row -- the per-row wrapper pair both hosts render (`transcriptRendering.tsx`,
 * `app-sdk/ChatMessageList.tsx`), with the clipping `.chat-message-body` -- so
 * this rebuilds that chain with the literal host classes and mounts the REAL
 * `UserMessage` and `AssistantMessage` inside it.
 *
 * Two rows, because the first cell is a different button on each and the two
 * columns clip for different reasons: the user row is end-aligned in a
 * shrink-to-fit column that a short bubble makes exactly as wide as the row,
 * and the assistant row starts at the column's start edge whenever no
 * timestamp leads it (timestamps off).
 *
 * `pull=on` re-applies the retired negative start margin on each row's first
 * cell, so one harness captures the BEFORE and AFTER states side by side and
 * the reproduction is asserted rather than assumed.
 * window.__measure() reports, per row, how many pixels of the first cell lie
 * start-side of the clipping column (`clippedStart`), which is the whole
 * assertion. Query string: ?theme=dark|light&pull=on|off
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { initI18n } from '../src/i18n'
import UserMessage from '../src/pages/chat/UserMessage'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const pullOn = params.get('pull') === 'on'

localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-mode', theme)
document.documentElement.setAttribute('data-pull', pullOn ? 'on' : 'off')

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

/**
 * The BEFORE state: a leading cell pulled 7px past the start of its clipping
 * column (10px on touch). Each row is matched by the `gap-x-0` token the
 * shared row constant carries, so the probe follows the production rows
 * rather than a selector of its own.
 */
const BEFORE_CSS = `
html[data-pull="on"] [class~="gap-x-0"] > button:first-child { margin-inline-start: -7px; }
@media (hover: none) {
  html[data-pull="on"] [class~="gap-x-0"] > button:first-child { margin-inline-start: -10px; }
}
`

const render = (c: string) => <MarkdownRenderer content={c} softBreaks />

/** Literal ChatPage / ChatMessageList per-row wrapper + row chain. */
function RowBox({ user, children }: { user?: boolean; children: React.ReactNode }) {
  return (
    <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 720 }}>
      <div className={`group flex flex-col min-w-0 ${user ? 'items-end' : ''}`}>
        <div className={`chat-message-body flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full ${user ? 'items-end' : ''}`}>
          {children}
        </div>
      </div>
    </div>
  )
}

function Scene() {
  useEffect(() => {
    if (!pullOn) return
    const style = document.createElement('style')
    style.textContent = BEFORE_CSS
    document.head.appendChild(style)
    return () => style.remove()
  }, [])
  return (
    <div className="bg-bg text-text chat-container flex flex-col py-3" style={{ width: 720 }} data-capture-root>
      {/* A SHORT user message: its end-aligned column is then exactly as wide
          as the action row, which is the shape that clips the leading cell.
          Quote leads the row, as in the report. */}
      <RowBox user>
        <UserMessage content="Ok." messageTs="2026-10-08T03:28:00Z" slotKey="chat-1" slotTitle="Action row" onTogglePin={() => {}} canEdit onEditResend={() => {}} messageIndex={0} renderContent={render} onQuoteMessage={() => {}} />
      </RowBox>
      {/* Timestamps off: a button leads the assistant row too. */}
      <RowBox>
        <AssistantMessage content="Done. The hover paint of the first action now stays inside the column." isStreaming={false} slotRunning={false} messageTs="2026-10-08T03:28:30Z" slotKey="chat-1" slotTitle="Action row" onTogglePin={() => {}} />
      </RowBox>
    </div>
  )
}

interface CellMeasure {
  /** The clipping column's start edge, in viewport px. */
  columnStart: number
  /** The leading cell's start edge and width. */
  cellStart: number
  cellWidth: number
  /** Pixels of the leading cell that lie start-side of the column: these are what overflow-hidden cuts off. */
  clippedStart: number
  /** The leading glyph's inset from the column start. */
  glyphInset: number
  marginInlineStart: string
}

declare global {
  interface Window {
    __measure: () => { user: CellMeasure; assistant: CellMeasure }
  }
}

function measureRow(role: 'user' | 'assistant'): CellMeasure {
  const root = document.querySelector<HTMLElement>(`[data-role="${role}"]`)!
  const column = root.closest<HTMLElement>('.chat-message-body')!
  const row = Array.from(root.querySelectorAll<HTMLElement>('div')).find(d => d.classList.contains('gap-x-0'))!
  const cell = row.querySelector<HTMLElement>(':scope > button:first-child')!
  const glyph = cell.querySelector<SVGElement>('svg')!
  const col = column.getBoundingClientRect()
  const c = cell.getBoundingClientRect()
  const g = glyph.getBoundingClientRect()
  return {
    columnStart: Math.round(col.left),
    cellStart: Math.round(c.left),
    cellWidth: Math.round(c.width),
    clippedStart: Math.max(0, Math.round(col.left - c.left)),
    glyphInset: Math.round(g.left - col.left),
    marginInlineStart: getComputedStyle(cell).marginInlineStart,
  }
}

window.__measure = () => ({ user: measureRow('user'), assistant: measureRow('assistant') })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <Scene />
  </MemoryRouter>,
)
