/**
 * The page's main thread, in the bottom-left corner of a doc or canvas (PoC): the
 * conversation about the page as a whole, beside the threads pinned to places on it.
 *
 * One box holds it all. Collapsed, it shows the last line of the latest reply above
 * the input, with the page's controls (approval mode, Plain / Crew, the session
 * view) as its footer. Expanded, it grows upward into the whole conversation.
 *
 * Labels are English like the rest of the doc conductor layer.
 */
import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { motion, useReducedMotion } from 'framer-motion'
import { ArrowUp, Braces, ChevronDown, ChevronUp, Loader, PanelBottom, PanelLeft, Sparkles, User } from 'lucide-react'
import { Btn } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import { ConductorControls } from './ConductorSession'
import { ANSWER_STYLE, type ThreadView } from './docConductor'
import { specToCss } from './styleSpec'
import { safeGetItem, safeSetItem } from '../../../utils/safeStorage'

// Whether the main thread's box is shown, the same on every page, kept across reloads.
const SHOWN_KEY = 'kc:main-thread:shown'
// Where the box sits: off to the side (bottom left), or front and center at the
// bottom like a chat's composer. The same on every page, kept across reloads.
const PLACE_KEY = 'kc:main-thread:place'

const SCOPE = '[data-main-thread]'
const DOCK_CSS = `${specToCss(ANSWER_STYLE, SCOPE)}
${SCOPE} [data-da="md"] ul{list-style:disc}
${SCOPE} [data-da="md"] ol{list-style:decimal}
${SCOPE} [data-da="md"] > :last-child{margin-bottom:0}`

/** The first readable line of a reply, without its markdown marks. */
export function peekLine(text: string): string {
  const line = text.split('\n').map(l => l.trim()).find(l => l && !l.startsWith('```')) ?? ''
  return line.replace(/^#+\s*|^[-*>]\s+|^\d+\.\s+/, '').replace(/[*_`]/g, '').replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
}

function Avatar({ who }: { who: 'you' | 'conductor' }) {
  return who === 'conductor'
    ? <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-accent text-accent-fg" aria-hidden><Sparkles size={13} /></span>
    : <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-bg-hover text-muted" aria-hidden><User size={13} /></span>
}

export default function MainThreadDock({ slotKey, approvalMode, sessionOpen, onToggleSession, thread, running, onSend, placeholder, controlsTestid, className, style, centerBottom = 'bottom-5' }: {
  slotKey: string
  approvalMode?: string
  sessionOpen: boolean
  onToggleSession: () => void
  /** The main thread as read from the transcript; undefined before anything was said. */
  thread: ThreadView | undefined
  /** The session is running a turn. */
  running: boolean
  onSend: (text: string) => void
  placeholder: string
  /** The controls row keeps the test id it had as a corner of its own. */
  controlsTestid: string
  /** Where a centered box's bottom sits: above a canvas's own tool dock, which holds the bottom center. */
  centerBottom?: string
  /** Stacking only: the box places itself. */
  className?: string
  style?: React.CSSProperties
}) {
  const [open, setOpen] = useState(false)
  const [shown, setShown] = useState(() => safeGetItem(SHOWN_KEY) !== '0')
  const [centered, setCentered] = useState(() => safeGetItem(PLACE_KEY) === 'center')
  const togglePlace = () => setCentered(v => { safeSetItem(PLACE_KEY, v ? 'side' : 'center'); return !v })
  const toggleShown = () => setShown(v => { safeSetItem(SHOWN_KEY, v ? '0' : '1'); if (v) setOpen(false); return !v })
  const [draft, setDraft] = useState('')
  const listRef = useRef<HTMLDivElement>(null)
  const reduceMotion = useReducedMotion()
  const read = thread?.messages ?? []
  const replies = read.filter(m => m.who === 'conductor').length
  // A send is not in the transcript until the gateway echoes it, so the box holds
  // its own: shown, and working, until a new reply lands or the turn it started ends.
  const [sent, setSent] = useState<{ text: string; replies: number; sawRunning: boolean } | null>(null)
  useEffect(() => {
    if (!sent) return
    if (replies > sent.replies || (sent.sawRunning && !running)) setSent(null)
    else if (running && !sent.sawRunning) setSent({ ...sent, sawRunning: true })
  }, [sent, replies, running])
  const echoed = !!sent && read.some((m, i) => m.who === 'you' && m.text === sent.text && read.slice(i + 1).every(n => n.who === 'you'))
  const messages = sent && !echoed ? [...read, { who: 'you' as const, text: sent.text }] : read
  const pending = !!thread?.pending || !!sent
  const lastReply = [...messages].reverse().find(m => m.who === 'conductor')
  const thinking = i18nT('pages.chat.chatFooter.thinking')
  const peek = pending ? thinking : lastReply ? peekLine(lastReply.text) : ''

  useEffect(() => {
    const el = listRef.current
    if (open && el) el.scrollTop = el.scrollHeight
  }, [open, messages.length, pending])

  const send = () => {
    const text = draft.trim()
    if (!text) return
    onSend(text)
    setSent({ text, replies, sawRunning: false })
    setDraft('')
  }

  const width = !shown ? '' : centered ? 'w-[min(var(--mc-content-width,760px),calc(100%-40px))]' : open ? 'w-[420px]' : 'w-[340px]'
  return (
    // The wrapper places the box; the box itself animates between its sizes.
    <div data-testid="main-thread" data-place={centered ? 'center' : 'side'} data-open={open} data-shown={shown} style={style}
      className={`${className ?? ''} pointer-events-none absolute ${centered ? `${centerBottom} inset-x-0 flex justify-center` : 'bottom-5 left-5'}`}>
    <motion.div
      layout={!reduceMotion}
      transition={{ duration: 0.18, ease: 'easeOut' }}
      data-main-thread=""
      className={`pointer-events-auto flex flex-col border border-border bg-bg-elevated text-text shadow-lg ${shown ? `rounded-2xl ${width}` : 'rounded-xl p-1'} max-w-[calc(100vw-40px)]`}
    >
      <style>{DOCK_CSS}</style>
      {shown && open && (
        <>
          <motion.div layout="position" className="flex items-center gap-2 border-b border-border px-3 py-2">
            <span className="flex-1 text-[13px] font-semibold">Main thread</span>
            {/* The raw session rows, a debugging view of what the page reads. */}
            <Btn type="button" onClick={onToggleSession} aria-pressed={sessionOpen} aria-label="Show the session's rows" title="Show the session's rows"
              data-testid="conductor-session-toggle"
              className={`h-7 w-7 justify-center border-none p-0 ${sessionOpen ? 'bg-bg-hover text-text' : 'text-muted hover:text-text'}`}>
              <Braces size={14} />
            </Btn>
            <Btn type="button" onClick={() => setOpen(false)} aria-label="Collapse the main thread" aria-expanded data-testid="main-thread-collapse"
              className="h-7 w-7 justify-center border-none p-0 text-muted hover:text-text">
              <ChevronDown size={16} />
            </Btn>
          </motion.div>
          <div ref={listRef} className={`${centered ? 'max-h-[60vh] min-h-[200px] px-5' : 'max-h-[50vh] min-h-[120px] px-3'} overflow-y-auto py-2`} aria-live="polite">
            {messages.length === 0 && !pending && (
              <p className="py-6 text-center text-[13px] text-muted">Talk about the page as a whole here.</p>
            )}
            <ol className="flex flex-col gap-0.5">
              {messages.map((m, i) => {
                const first = i === 0 || messages[i - 1].who !== m.who
                return (
                  <li key={i} data-testid="main-thread-message" data-who={m.who} className={`flex gap-2.5 ${first ? 'pt-2' : ''}`}>
                    {first ? <Avatar who={m.who} /> : <span className="w-6 shrink-0" aria-hidden />}
                    <div className="min-w-0 flex-1">
                      {first && <div className="text-[12.5px] font-bold leading-tight">{m.who === 'you' ? 'You' : 'Conductor'}</div>}
                      {m.who === 'conductor'
                        ? <div data-da="md" className="mt-0.5 text-[13.5px] leading-relaxed"><ReactMarkdown remarkPlugins={[remarkGfm]}>{m.text}</ReactMarkdown></div>
                        : <div className="mt-0.5 whitespace-pre-wrap text-[13.5px] leading-relaxed">{m.text}</div>}
                    </div>
                  </li>
                )
              })}
            </ol>
            {pending && (
              <div className="flex items-center gap-2.5 pt-2 text-[13px] text-muted" data-testid="main-thread-pending">
                <span className="flex w-6 shrink-0 justify-center text-accent"><Loader size={14} className="motion-safe:animate-spin" aria-hidden /></span>
                <span className="text-accent">{thinking}</span>
              </div>
            )}
          </div>
        </>
      )}

      {shown && !open && peek && (
        <Btn type="button" onClick={() => setOpen(true)} aria-label="Expand the main thread" aria-expanded={false} data-testid="main-thread-peek"
          title="Show the whole conversation"
          className="mx-1 mt-1 h-8 justify-start gap-2 rounded-lg border-none px-2 text-[12.5px] text-muted hover:bg-bg-hover">
          {pending
            ? <span className="flex w-6 shrink-0 justify-center text-accent" data-testid="main-thread-spinner"><Loader size={14} className="motion-safe:animate-spin" aria-hidden /></span>
            : <Avatar who="conductor" />}
          <span className={`min-w-0 flex-1 truncate text-left${pending ? ' text-accent' : ''}`}>{peek}</span>
          <ChevronUp size={14} aria-hidden />
        </Btn>
      )}

      {shown && <motion.div layout="position" className="flex items-end gap-1.5 px-3 pb-1 pt-2">
        <textarea
          data-testid="main-thread-input"
          aria-label="Message the main thread"
          rows={1}
          placeholder={placeholder}
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send() }
            else if (e.key === 'Escape' && open) { e.preventDefault(); setOpen(false) }
          }}
          className="max-h-32 min-h-[24px] flex-1 resize-none border-none bg-transparent py-1 text-[13.5px] text-text outline-none [field-sizing:content] placeholder:text-muted"
        />
        {!open && !peek && (
          <Btn type="button" onClick={() => setOpen(true)} aria-label="Expand the main thread" aria-expanded={false} data-testid="main-thread-expand"
            className="h-7 w-7 justify-center border-none p-0 text-muted hover:text-text">
            <ChevronUp size={15} />
          </Btn>
        )}
        <Btn type="button" onClick={send} disabled={!draft.trim()} aria-label="Send to the main thread" data-testid="main-thread-send"
          className={`h-7 w-7 justify-center rounded-md border-none p-0 ${draft.trim() ? 'bg-accent text-accent-fg hover:bg-accent-hover' : 'text-muted'}`}>
          <ArrowUp size={15} />
        </Btn>
      </motion.div>}

      {/* The page's controls are the box's footer. */}
      <motion.div layout="position" className={`relative flex items-center${shown ? ' mx-1 mb-1 border-t border-border pt-1' : ''}`}>
        <ConductorControls slotKey={slotKey} approvalMode={approvalMode} crewSwitch
          sessionOpen={shown} onToggleSession={toggleShown} toggleTestid="main-thread-toggle"
          toggleLabel={shown ? 'Hide the main thread' : 'Show the main thread'}
          className="" testid={controlsTestid} />
        {shown && (
          <Btn type="button" onClick={togglePlace} aria-pressed={centered} data-testid="main-thread-place"
            aria-label={centered ? 'Move the main thread to the side' : 'Move the main thread to the center'}
            title={centered ? 'Move to the side' : 'Move to the center'}
            className="ml-auto h-7 w-7 justify-center border-none p-0 text-muted hover:text-text">
            {centered ? <PanelLeft size={14} /> : <PanelBottom size={14} />}
          </Btn>
        )}
      </motion.div>
    </motion.div>
    </div>
  )
}
