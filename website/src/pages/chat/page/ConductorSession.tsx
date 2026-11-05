/**
 * A look inside the conductor's session from a docwork page: the slot's message rows
 * as the page holds them, in order, each with the thread the page reads it into. A
 * docwork page has no transcript of its own, so this is the one place to see how a
 * highlight, a goal wake or a reply lands in the conversation the page reads.
 *
 * It shows the client's copy of the session. The model also receives what the
 * gateway adds to each turn (system prompt, memory, skills), which no row carries.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Lock, ScrollText, User, Users, X } from 'lucide-react'
import ApprovalModePicker from '../../../components/ApprovalModePicker'
import { Btn } from '../../../components/ui'
import { useAppDispatch, useAppSelector } from '../../../store'
import { selectSlotMessages } from '../../../store/chat/selectors'
import type { ChatMessage } from '../../../types'
import { openedThread } from './docThreads'
import ErrorNotice from '../../../components/ErrorNotice'
import { PAGE_WORKER_AGENT, crewModeAgent, runsCrewMode } from '../../../lib/crewMode'
import { performAgentSlotSwitch } from '../../../lib/agentSwitch'
import { slotRowByKey } from '../../../store/slotIndex'

/** The two ways a page can run, as the person reads them. */
const PAGE_MODES = [
  { id: 'plain', label: 'Plain', icon: User, help: 'One agent does the work itself, on this page.' },
  { id: 'crew', label: 'Crew', icon: Users, help: 'A conductor runs a crew of workers for this page.' },
] as const

/**
 * How the page runs: Plain (the worker does the work itself) or Crew (the conductor
 * runs a crew). Chosen before the page is first asked anything; from then on it is
 * fixed, in either direction, and shows as a label.
 */
function PageModeChoice({ slotKey }: { slotKey: string }) {
  const dispatch = useAppDispatch()
  const row = useAppSelector(s => slotRowByKey(s.dashboard.slots, slotKey))
  const asked = useAppSelector(s => selectSlotMessages(s, slotKey).some(m => m.role === 'user'))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const current = runsCrewMode(row?.agent, row?.agent_kind, row?.crew_mode_agent) ? 'crew' : 'plain'
  const mode = PAGE_MODES.find(m => m.id === current)!
  const choose = (id: 'plain' | 'crew') => {
    if (id === current || busy) return
    setError('')
    setBusy(true)
    performAgentSlotSwitch(slotKey, id === 'crew' ? crewModeAgent() : PAGE_WORKER_AGENT, dispatch, 'template')
      .catch(err => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setBusy(false))
  }
  if (asked) {
    const Icon = mode.icon
    return (
      <span data-testid="page-mode" data-mode={current} title={`${mode.help} Set when this page was first asked; it stays.`}
        className="flex h-7 items-center gap-1.5 rounded-md px-2 text-[12px] text-muted">
        <Icon size={14} aria-hidden /> {mode.label}
        <Lock size={11} aria-hidden className="opacity-60" />
      </span>
    )
  }
  return (
    <>
      <span role="radiogroup" aria-label="How this page runs" data-testid="page-mode" data-mode={current}
        className="flex items-center rounded-lg border border-border p-0.5">
        {PAGE_MODES.map(m => {
          const Icon = m.icon
          const on = m.id === current
          return (
            <Btn key={m.id} type="button" role="radio" aria-checked={on} disabled={busy} title={m.help}
              data-testid={`page-mode-${m.id}`} onClick={() => choose(m.id)}
              className={`h-6 border-none px-2 text-[12px] ${on ? 'bg-bg-hover text-text' : 'text-muted'}`}>
              <Icon size={13} aria-hidden /> {m.label}
            </Btn>
          )
        })}
      </span>
      {/* No hand-off: choosing again is one press, and nothing is pending. */}
      {error && <ErrorNotice title="The page's mode could not be changed" message={error} className="absolute bottom-full left-0 mb-2 w-[320px]" />}
    </>
  )
}

/** The page's trust picker with the session view's toggle beside it. Positioned by
 *  the caller, which keeps its own corner and test id. */
export function ConductorControls({ slotKey, approvalMode, sessionOpen, onToggleSession, className, style, testid, crewSwitch = false, toggleLabel = "Show the conductor's session", toggleTestid = 'conductor-session-toggle' }: {
  slotKey: string
  /** What the scroll button toggles, when it is not the session view (a page's main thread). */
  toggleLabel?: string
  toggleTestid?: string
  /** Offer the page's Plain / Crew choice (docs and boards). */
  crewSwitch?: boolean
  approvalMode?: string
  sessionOpen: boolean
  onToggleSession: () => void
  className: string
  style?: React.CSSProperties
  testid: string
}) {
  return (
    <div className={`${className} flex items-center gap-1`} style={style} data-testid={testid}>
      {approvalMode && <ApprovalModePicker mode={approvalMode} slotKey={slotKey} />}
      {crewSwitch && <PageModeChoice slotKey={slotKey} />}
      <Btn
        type="button"
        onClick={onToggleSession}
        aria-pressed={sessionOpen}
        aria-label={toggleLabel}
        title={toggleLabel}
        data-testid={toggleTestid}
        className={`h-7 border-none px-2 text-[12px] ${sessionOpen ? 'bg-bg-hover text-text' : 'text-muted'}`}
      >
        <ScrollText size={14} />
      </Btn>
    </div>
  )
}

/** Each row with the thread it belongs to, by the same rule the page reads with. */
function labelRows(messages: ChatMessage[]): { m: ChatMessage; thread: string | null; opens: boolean }[] {
  let current: string | null = null
  return messages.map(m => {
    const opened = openedThread(m)
    if (opened !== undefined) current = opened
    return { m, thread: current, opens: opened !== undefined }
  })
}

const ROLE_TONE: Record<string, string> = {
  user: 'text-accent',
  nudge: 'text-warn-text',
  assistant: 'text-ok-text',
}

function Row({ index, m, thread, opens }: { index: number; m: ChatMessage; thread: string | null; opens: boolean }) {
  const [open, setOpen] = useState(false)
  const text = m.content ?? ''
  const meta = m.meta && Object.keys(m.meta).length ? JSON.stringify(m.meta, null, 2) : ''
  return (
    <li className={`border-b border-border px-3 py-2 ${opens ? 'bg-bg-hover/40' : ''}`} data-testid="conductor-session-row">
      <div className="flex items-center gap-2 text-[11px]">
        <span className="tabular-nums text-muted">#{index}</span>
        <span className={`font-mono font-semibold ${ROLE_TONE[m.role] ?? 'text-muted'}`}>{m.role}</span>
        {m.kind && <span className="font-mono text-muted">{m.kind}</span>}
        <span className="min-w-0 flex-1 truncate font-mono text-muted" title={thread ?? undefined}>
          {thread ? `${opens ? 'opens' : 'in'} ${thread}` : opens ? 'opens no thread' : ''}
        </span>
        {m.ts && <span className="shrink-0 text-muted">{m.ts.slice(11, 19)}</span>}
      </div>
      <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-snug text-text">{text || '(empty)'}</pre>
      {meta && (
        <>
          <Btn type="button" onClick={() => setOpen(o => !o)} aria-expanded={open}
            className="mt-1 h-6 border-none px-1 text-[11px] text-muted">
            {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />} meta
          </Btn>
          {open && <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-muted">{meta}</pre>}
        </>
      )}
    </li>
  )
}

/** The session view: a panel over the page's left side, following new rows while
 *  the reader is at the bottom. */
export function SessionMessagesPanel({ slotKey, onClose }: { slotKey: string; onClose: () => void }) {
  const messages = useAppSelector(s => selectSlotMessages(s, slotKey))
  const rows = useMemo(() => labelRows(messages), [messages])
  const listRef = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  useEffect(() => {
    const el = listRef.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [rows.length])
  return (
    <aside
      className="absolute bottom-0 left-0 top-0 flex w-[460px] max-w-[90vw] flex-col border-r border-border bg-bg-elevated text-text shadow-lg"
      style={{ zIndex: 24 }}
      aria-label="Conductor session"
      data-testid="conductor-session"
    >
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <ScrollText size={14} className="text-muted" />
        <span className="text-[13px] font-semibold">Conductor session</span>
        <span className="text-[11px] text-muted">{rows.length} rows · {slotKey}</span>
        <span className="flex-1" />
        <Btn type="button" onClick={onClose} aria-label="Close the conductor's session" className="h-7 border-none px-1.5 text-muted">
          <X size={14} />
        </Btn>
      </div>
      <div
        ref={listRef}
        className="flex-1 overflow-y-auto pb-16"
        onScroll={e => { const el = e.currentTarget; pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40 }}
      >
        {rows.length === 0
          ? <p className="px-3 py-4 text-[12px] text-muted">No messages in this session yet.</p>
          : <ol aria-live="polite">{rows.map((r, i) => <Row key={`${i}:${r.m.ts ?? ''}`} index={i} {...r} />)}</ol>}
      </div>
    </aside>
  )
}
