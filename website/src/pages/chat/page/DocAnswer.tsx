/**
 * The conductor's answer to one highlight, drawn under it in the doc pane (PoC).
 *
 * Collapsed by default to one line: who, the latest word, unread and worker
 * counts. Only a click opens it; the editor's caret never enters it (it is a
 * block widget, not text). Open, it shows the thread with the replies rendered
 * as markdown, the workers the conductor started for it, and a reply box.
 *
 * Styled by the editor theme in `docConductor` through `data-da` parts, so the
 * look lives beside the widget that hosts it.
 */
import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ThreadView, WorkerState } from './docConductor'

const WORKER_LABEL: Record<WorkerState, string> = { working: 'Working', needs_you: 'Needs you', idle: 'Idle' }

function firstLine(text: string): string {
  const line = text.split('\n').map(l => l.replace(/[#>*_`|-]/g, ' ').trim()).find(Boolean) ?? ''
  return line.length > 120 ? `${line.slice(0, 117)}…` : line
}

export default function DocAnswer({
  thread, onOpen, onSend, onOpenSession, compact = false, active = false, focusSignal = 0, onActivate,
}: {
  thread: ThreadView | undefined
  onOpen: () => void
  onSend: (text: string) => void
  onOpenSession: (key: string) => void
  /** A small pin (a board's answers) instead of a full-width bar; the thread
   *  opens as a floating card rather than pushing anything down. */
  compact?: boolean
  /** The page's current conversation: another becoming current collapses this one. */
  active?: boolean
  /** Changes when the conversation bar or its shortcut moves focus here: open,
   *  scroll into view, and put the caret in the reply box. 0 = never. */
  focusSignal?: number
  /** This conversation was opened by a click: it becomes the current one. */
  onActivate?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const rootRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const onOpenRef = useRef(onOpen)
  onOpenRef.current = onOpen

  useEffect(() => {
    if (!focusSignal) return
    setOpen(true)
    onOpenRef.current()
    // After the body renders, so the reply box exists and the scroll lands on it.
    const raf = requestAnimationFrame(() => {
      rootRef.current?.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
      inputRef.current?.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(raf)
  }, [focusSignal])

  useEffect(() => { if (!active) setOpen(false) }, [active])
  const messages = thread?.messages ?? []
  const workers = thread?.workers ?? []
  const pending = thread ? thread.pending : true
  const latest = [...messages].reverse().find(m => m.who === 'conductor')
  const running = workers.filter(w => w.state === 'working').length
  const summary = pending ? 'Thinking…' : latest ? firstLine(latest.text) : 'No reply yet'
  const dotState = pending ? 'pending' : thread?.unread ? 'unread' : 'read'
  const pinLabel = pending ? 'Thinking…' : thread?.unread ? 'New reply' : latest ? 'Reply' : 'No reply'

  return (
    <div ref={rootRef} data-da="root" data-testid="doc-answer" data-open={open ? 'true' : 'false'} data-active={active ? 'true' : 'false'} data-compact={compact ? 'true' : 'false'}>
      <button
        type="button"
        data-da="bar"
        data-testid="doc-answer-bar"
        aria-expanded={open}
        title={compact ? summary : undefined}
        onMouseDown={e => e.preventDefault()}
        onClick={() => { setOpen(o => !o); if (!open) { onOpen(); onActivate?.() } }}
      >
        <span data-da="dot" data-state={dotState} aria-hidden />
        <span data-da="who">{compact ? '✦' : '✦ Conductor'}</span>
        {thread?.topic && <span data-da="chip" data-testid="doc-answer-topic">{thread.topic}</span>}
        <span data-da="summary">{compact ? pinLabel : summary}</span>
        {workers.length > 0 && (
          <span data-da="chip">{`${running}/${workers.length} working`}</span>
        )}
        {!compact && <span data-da="caret" aria-hidden>{open ? '▾' : '▸'}</span>}
      </button>
      {open && (
        <div data-da="body">
          {messages.map((m, i) => (
            <div key={i} data-da="msg" data-who={m.who}>
              <div data-da="msg-who">{m.who === 'you' ? 'You' : 'Conductor'}</div>
              {m.who === 'conductor'
                ? <div data-da="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{m.text}</ReactMarkdown></div>
                : <div data-da="text">{m.text}</div>}
            </div>
          ))}
          {pending && <div data-da="waiting">Conductor is working on it…</div>}
          {workers.length > 0 && (
            <div data-da="workers">
              {workers.map(w => (
                <button key={w.key} type="button" data-da="worker" data-state={w.state} data-testid="doc-answer-worker" onClick={() => onOpenSession(w.key)}>
                  <span data-da="dot" data-state={w.state} aria-hidden />
                  <span>{w.title}</span>
                  <span data-da="worker-state">{WORKER_LABEL[w.state]}</span>
                </button>
              ))}
            </div>
          )}
          <textarea
            ref={inputRef}
            data-da="input"
            data-testid="doc-answer-input"
            aria-label="Reply to the conductor"
            rows={2}
            placeholder="Reply to the conductor…"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                const text = draft.trim()
                if (text) { onSend(text); setDraft('') }
              } else if (e.key === 'Escape') {
                e.preventDefault()
                setOpen(false)
              }
            }}
          />
        </div>
      )}
    </div>
  )
}
