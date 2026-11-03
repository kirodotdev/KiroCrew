/**
 * One conversation with the conductor, drawn on the board (PoC): a pin where the
 * person pointed, a small preview when the pointer rests on it, and a thread card
 * when it is opened. The card holds the conversation, the workers the conductor
 * started for it, and a reply field.
 *
 * Placed by its parent (absolute, at the pin's screen position); this draws the
 * pin at its origin and the card to its right. Labels are hardcoded English like
 * the rest of the board canvas.
 */
import { ArrowUp, MoreHorizontal, Sparkles, User, X } from 'lucide-react'
import { motion, useReducedMotion } from 'framer-motion'
import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ThreadView, WorkerState } from '../docConductor'

const WORKER_LABEL: Record<WorkerState, string> = { working: 'Working', needs_you: 'Needs you', idle: 'Idle' }

/** The first line of a reply, without markdown, for the pin's preview. */
export function firstLine(text: string): string {
  // Emphasis and code marks go; list, quote and heading markers become a space; runs of space collapse.
  const line = text.split('\n').map(l => l.replace(/[*_`]/g, '').replace(/[#>|-]/g, ' ').replace(/\s+/g, ' ').trim()).find(Boolean) ?? ''
  return line.length > 140 ? `${line.slice(0, 137)}…` : line
}

interface Props {
  thread: ThreadView | undefined
  /** What the conversation is about, for the card's header: "To do", "Whole board", a card's text. */
  about: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onSend: (text: string) => void
  onDelete: () => void
  onOpenSession: (key: string) => void
  /** Which side of the pin the preview and card open on: the parent picks the one with room. */
  side?: 'right' | 'left'
  /** How far to lift the card so it stays above the bottom of the board. */
  lift?: number
}

/** The card's width and its tallest, so a parent can tell whether it fits. */
export const CARD_W = 380
export const CARD_MAX_H = 460

const WORKER_DOT: Record<WorkerState, string> = { working: 'var(--accent)', needs_you: 'var(--warn)', idle: 'var(--muted)' }

/** Markdown in a reply: paragraph and list spacing, and light tables that fit the card. */
const MD = [
  '[&_p]:mb-2 [&_p:last-child]:mb-0 [&_ul]:mb-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:mb-2 [&_ol]:list-decimal [&_ol]:pl-5 [&_li]:mb-0.5',
  '[&_strong]:font-semibold [&_code]:rounded [&_code]:bg-bg-hover [&_code]:px-1 [&_code]:text-[12.5px]',
  '[&_h1]:mb-1.5 [&_h1]:font-semibold [&_h2]:mb-1.5 [&_h2]:font-semibold [&_h3]:mb-1 [&_h3]:font-semibold',
  '[&_table]:my-2 [&_table]:w-full [&_table]:border-collapse [&_table]:text-[12.5px]',
  '[&_th]:border-b [&_th]:border-border [&_th]:py-1 [&_th]:pr-3 [&_th]:text-left [&_th]:font-medium [&_th]:text-muted',
  '[&_td]:border-b [&_td]:border-border/50 [&_td]:py-1 [&_td]:pr-3',
  '[&_a]:text-accent [&_a]:underline',
].join(' ')

/** Who wrote a message: the conductor's mark, or yours. */
function Avatar({ who }: { who: 'you' | 'conductor' }) {
  return who === 'conductor'
    ? <span aria-hidden className="flex h-7 w-7 flex-none items-center justify-center rounded-full bg-accent text-accent-fg"><Sparkles size={14} /></span>
    : <span aria-hidden className="flex h-7 w-7 flex-none items-center justify-center rounded-full bg-bg-hover text-muted ring-1 ring-border"><User size={15} /></span>
}

/** The pin: a speech bubble with its tail at the bottom-left, which is the point it marks. */
export function PinGlyph({ state }: { state: 'pending' | 'unread' | 'read' }) {
  const reduce = useReducedMotion()
  return (
    <span className="relative block" style={{ width: 28, height: 28 }}>
      {state === 'pending' && !reduce && (
        <motion.span aria-hidden className="absolute inset-0" style={{ borderRadius: '50% 50% 50% 4px', background: 'var(--accent)' }}
          animate={{ scale: [1, 1.7], opacity: [0.45, 0] }} transition={{ duration: 1.4, repeat: Infinity, ease: 'easeOut' }} />
      )}
      <span className="relative flex h-full w-full items-center justify-center text-accent-fg shadow-md"
        style={{ borderRadius: '50% 50% 50% 4px', background: 'var(--accent)', border: '2px solid var(--bg)' }}>
        <Sparkles size={13} />
      </span>
      {state === 'unread' && (
        <span aria-hidden className="absolute -right-0.5 -top-0.5 block rounded-full" data-testid="thread-unread"
          style={{ width: 10, height: 10, background: 'var(--danger)', border: '2px solid var(--bg)' }} />
      )}
    </span>
  )
}

export default function ConductorThread({ thread, about, open, onOpenChange, onSend, onDelete, onOpenSession, side = 'right', lift = 0 }: Props) {
  const beside = side === 'right' ? 'left-9' : 'right-9'
  const [hover, setHover] = useState(false)
  const [menu, setMenu] = useState(false)
  const [draft, setDraft] = useState('')
  const input = useRef<HTMLTextAreaElement>(null)
  const scroller = useRef<HTMLDivElement>(null)

  const messages = thread?.messages ?? []
  const workers = thread?.workers ?? []
  const pending = thread ? thread.pending : true
  const state: 'pending' | 'unread' | 'read' = pending ? 'pending' : thread?.unread ? 'unread' : 'read'
  const latest = [...messages].reverse().find(m => m.who === 'conductor')
  const replies = messages.filter(m => m.who === 'conductor').length
  const working = workers.filter(w => w.state === 'working').length
  const status = pending ? 'Thinking…' : latest ? firstLine(latest.text) : 'No reply yet'

  // Opening puts the caret in the reply field and shows the newest message.
  useEffect(() => {
    if (!open) { setMenu(false); return }
    const raf = requestAnimationFrame(() => input.current?.focus({ preventScroll: true }))
    return () => cancelAnimationFrame(raf)
  }, [open])
  useEffect(() => { if (open && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight }, [open, messages.length, pending])

  const send = () => {
    const text = draft.trim()
    if (!text) return
    onSend(text)
    setDraft('')
  }

  return (
    <div data-testid="conductor-thread" data-open={open ? 'true' : 'false'} data-state={state} className="relative">
      <button type="button" data-testid="conductor-pin" aria-expanded={open} aria-label={`Conversation with the conductor: ${status}`}
        onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)} onFocus={() => setHover(true)} onBlur={() => setHover(false)}
        onMouseDown={e => e.preventDefault()} onClick={() => { setHover(false); onOpenChange(!open) }} className="block cursor-pointer">
        <PinGlyph state={state} />
      </button>

      {hover && !open && (
        <div role="tooltip" data-testid="conductor-preview" className={`pointer-events-none absolute ${beside} top-0 w-[260px] rounded-2xl border border-border bg-bg-elevated px-3 py-2 shadow-xl`}>
          <div className="text-[13px] text-text" style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{status}</div>
          <div className="mt-0.5 flex gap-2 text-[11px] text-muted">
            <span>{replies === 0 ? 'No replies' : replies === 1 ? '1 reply' : `${replies} replies`}</span>
            {workers.length > 0 && <span>{working}/{workers.length} working</span>}
          </div>
        </div>
      )}

      {open && (
        <div role="dialog" aria-label="Conversation with the conductor" data-testid="conductor-card" data-side={side}
          className={`absolute ${beside} flex flex-col rounded-2xl border border-border/60 bg-bg-elevated shadow-[0_12px_40px_-8px_rgba(0,0,0,.35)]`}
          style={{ top: -lift, width: CARD_W, maxHeight: CARD_MAX_H }}>
          <div className="flex items-center gap-1 border-b border-border/70 py-3 pl-5 pr-3">
            <span className="flex-1 text-sm font-semibold text-text" title={`About ${about}`}>Conductor</span>
            <div className="relative">
              <button type="button" aria-label="More" aria-expanded={menu} onClick={() => setMenu(m => !m)} className="rounded-md p-1.5 text-text hover:bg-bg-hover">
                <MoreHorizontal size={18} />
              </button>
              {menu && (
                <div role="menu" className="absolute right-0 top-full z-10 mt-1 w-[190px] rounded-xl border border-border bg-bg-elevated p-1 shadow-xl">
                  <button type="button" role="menuitem" data-testid="conductor-delete" onClick={() => { setMenu(false); onDelete() }}
                    className="w-full rounded-lg px-2.5 py-1.5 text-left text-[13px] text-danger hover:bg-bg-hover">
                    Delete conversation
                  </button>
                </div>
              )}
            </div>
            <button type="button" aria-label="Close" onClick={() => onOpenChange(false)} className="rounded-md p-1.5 text-text hover:bg-bg-hover">
              <X size={18} />
            </button>
          </div>

          {/* One row per message, the same for both sides: who on top, the text indented under it. */}
          <div ref={scroller} className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-5 pb-2 pt-4" aria-live="polite">
            <div className="-mt-1 text-[12px] text-muted" data-testid="thread-about">About {about}</div>
            {messages.map((m, i) => (
              // A run of messages from one side shares its header, so the card does not repeat itself.
              <div key={i} data-testid="thread-msg" data-who={m.who} className={i > 0 && messages[i - 1].who === m.who ? '-mt-3' : undefined}>
                {(i === 0 || messages[i - 1].who !== m.who) && (
                  <div className="mb-1.5 flex items-center gap-2.5">
                    <Avatar who={m.who} />
                    <span className="text-[13.5px] font-semibold text-text">{m.who === 'you' ? 'You' : 'Conductor'}</span>
                  </div>
                )}
                <div className="pl-[38px] text-[13.5px] leading-[1.6] text-text">
                  {m.who === 'conductor'
                    ? <div className={MD}><ReactMarkdown remarkPlugins={[remarkGfm]}>{m.text}</ReactMarkdown></div>
                    : <div className="whitespace-pre-wrap">{m.text}</div>}
                </div>
              </div>
            ))}
            {pending && (
              <div data-testid="thread-waiting" className="flex items-center gap-2.5">
                <Avatar who="conductor" />
                <span className="text-[13px] text-muted">Working on it…</span>
              </div>
            )}
            {workers.length > 0 && (
              <div className="ml-[38px] flex flex-col gap-0.5 rounded-xl bg-bg-hover/60 p-1.5">
                <div className="px-2 pb-0.5 pt-1 text-[11px] font-medium uppercase tracking-[.04em] text-muted">Workers</div>
                {workers.map(w => (
                  <button key={w.key} type="button" data-state={w.state} data-testid="thread-worker" onClick={() => onOpenSession(w.key)} title="Open its session"
                    className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[13px] text-text hover:bg-bg-elevated">
                    <span aria-hidden className="block h-2 w-2 flex-none rounded-full" style={{ background: WORKER_DOT[w.state] }} />
                    <span className="min-w-0 flex-1 truncate">{w.title}</span>
                    <span className="flex-none text-[11px] text-muted">{WORKER_LABEL[w.state]}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* The reply: your avatar, then one pill with the send arrow inside it. */}
          <div className="flex items-end gap-2.5 px-5 pb-4 pt-2">
            <Avatar who="you" />
            <div className="relative min-w-0 flex-1">
              <textarea ref={input} rows={1} value={draft} data-testid="thread-reply" aria-label="Reply to the conductor" placeholder="Reply"
                onChange={e => setDraft(e.target.value)}
                onKeyDown={e => {
                  e.stopPropagation()
                  if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send() }
                  else if (e.key === 'Escape') { e.preventDefault(); onOpenChange(false) }
                }}
                className="block max-h-28 min-h-[40px] w-full resize-none rounded-[20px] border border-border bg-bg-hover/50 py-[9px] pl-4 pr-11 text-[13.5px] leading-[20px] text-text outline-none placeholder:text-muted focus:border-accent" />
              <button type="button" aria-label="Send reply" data-testid="thread-send" disabled={!draft.trim()} onClick={send}
                className="absolute bottom-[5px] right-[5px] flex h-[30px] w-[30px] items-center justify-center rounded-full bg-accent text-accent-fg disabled:bg-border disabled:text-muted">
                <ArrowUp size={16} />
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
