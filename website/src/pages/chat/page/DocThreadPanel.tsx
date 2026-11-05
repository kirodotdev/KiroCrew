/**
 * A doc thread in the page's side panel, the way a Slack thread opens beside the
 * channel (PoC). The document stays whole in the middle; the conversation about one
 * highlight sits on the right: the highlight as the thread's root post, a replies
 * divider, the messages as avatar rows, the workers it started, and a composer.
 *
 * Labels are English like the rest of the doc conductor layer.
 */
import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ArrowUp, Quote, Sparkles, Target, User, X } from 'lucide-react'
import { Btn } from '../../../components/ui'
import ResizeHandle from '../../../components/ResizeHandle'
import { useColumnResize } from '../../../hooks/useColumnResize'
import { i18nT } from '../../../i18n/t'
import { safeGetItem } from '../../../utils/safeStorage'
import { ANSWER_STYLE, type ThreadView, type WorkerState } from './docConductor'
import { specToCss } from './styleSpec'

const SCOPE = '[data-doc-thread-panel]'
// The answer styles, plus list markers: the app's reset removes them, and a list in a
// reply reads as one without them.
const PANEL_CSS = `${specToCss(ANSWER_STYLE, SCOPE)}
${SCOPE} [data-da="md"] ul{list-style:disc}
${SCOPE} [data-da="md"] ol{list-style:decimal}
${SCOPE} [data-da="md"] > :last-child{margin-bottom:0}`

// The panel's width, dragged from its left edge and kept across threads and reloads.
const WIDTH_KEY = 'kc:doc-thread-panel:width'
const MIN_WIDTH = 320
const MAX_WIDTH = 760
const DEFAULT_WIDTH = 420
const loadWidth = () => {
  const raw = Number(safeGetItem(WIDTH_KEY))
  return Number.isFinite(raw) && raw > 0 ? Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, raw)) : DEFAULT_WIDTH
}

const WORKER_LABEL: Record<WorkerState, string> = { working: 'Working', needs_you: 'Needs you', idle: 'Idle' }

function Avatar({ who }: { who: 'you' | 'conductor' }) {
  return who === 'conductor'
    ? <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent text-accent-fg" aria-hidden><Sparkles size={16} /></span>
    : <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-bg-hover text-muted" aria-hidden><User size={16} /></span>
}

export default function DocThreadPanel({ thread, about, isGoal, title, focusSignal, onSend, onClose, onOpenSession }: {
  thread: ThreadView | undefined
  /** What the thread is about: the highlighted text, or a goal's section. */
  about: string
  isGoal: boolean
  /** The document's title, shown under "Thread" as Slack shows the channel. */
  title?: string
  /** Changes when the conversation bar or its shortcut moves here: the reply box takes the caret. */
  focusSignal: number
  onSend: (text: string) => void
  onClose: () => void
  onOpenSession: (key: string) => void
}) {
  const [draft, setDraft] = useState('')
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const messages = thread?.messages ?? []
  const workers = thread?.workers ?? []
  const pending = thread ? thread.pending : true
  const replies = messages.filter(m => m.who === 'conductor').length
  const resize = useColumnResize(WIDTH_KEY, loadWidth, MIN_WIDTH, MAX_WIDTH, undefined, undefined, 'left')

  useEffect(() => {
    if (focusSignal) inputRef.current?.focus({ preventScroll: true })
  }, [focusSignal])
  // A new message keeps the newest in view, as a chat does.
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages.length, pending])

  const send = () => {
    const text = draft.trim()
    if (!text) return
    onSend(text)
    setDraft('')
  }

  const RootIcon = isGoal ? Target : Quote
  return (
    // The session's title row overlays the top of the pane, so the panel starts
    // below it and its header (and close button) stay in reach.
    <aside data-doc-thread-panel="" data-testid="doc-thread-panel" aria-label="Thread"
      style={{ width: resize.width }}
      className="relative flex h-full max-w-[60vw] shrink-0 flex-col border-l border-border bg-bg pt-12 text-text">
      <style>{PANEL_CSS}</style>
      {/* The grip rides the panel's left border, below the session title row. */}
      <ResizeHandle handleProps={resize.handleProps} onNudge={resize.nudge} label={i18nT('pages.chat.sidePanel.resize_panel')}
        value={resize.width} min={MIN_WIDTH} max={MAX_WIDTH} className="absolute bottom-0 -left-[3px] top-12 z-10 h-auto" />
      <header className="flex items-center gap-3 border-b border-border px-5 py-3">
        <div className="min-w-0 flex-1">
          <div className="text-[15px] font-bold leading-tight">{isGoal ? 'Goal thread' : 'Thread'}</div>
          {title && <div className="truncate text-[12px] text-muted">{title}</div>}
        </div>
        <Btn type="button" onClick={onClose} aria-label="Close the thread" title="Close (Esc)" data-testid="doc-thread-close"
          className="h-8 w-8 justify-center rounded-md border-none p-0 text-muted hover:text-text">
          <X size={18} />
        </Btn>
      </header>

      <div ref={listRef} className="flex-1 overflow-y-auto py-3">
        {/* The root post: what the thread is about. */}
        <div className="flex gap-3 px-5 py-2">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent/15 text-accent" aria-hidden><RootIcon size={15} /></span>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-semibold">{isGoal ? 'Goal section' : 'Highlight'}</div>
            <blockquote data-testid="doc-thread-about"
              className="mt-1 max-h-48 overflow-hidden whitespace-pre-wrap rounded-md border-l-[3px] border-accent/60 bg-bg-hover/60 px-3 py-2 text-[13.5px] leading-snug text-text">
              {about.trim() || (isGoal ? 'This goal\'s section' : 'A spot in the document, with nothing highlighted')}
            </blockquote>
          </div>
        </div>

        {/* The replies divider, as Slack draws it under a thread's root. */}
        <div className="my-2 flex items-center gap-3 px-5" aria-hidden>
          <span className="text-[12px] font-medium text-muted">{replies === 0 ? 'No replies yet' : replies === 1 ? '1 reply' : `${replies} replies`}</span>
          <span className="h-px flex-1 bg-border" />
        </div>

        <ol className="flex flex-col" aria-live="polite">
          {messages.map((m, i) => {
            // A run of messages from one sender shows its name and avatar once.
            const first = i === 0 || messages[i - 1].who !== m.who
            return (
              <li key={i} data-testid="doc-thread-message" data-who={m.who}
                className={`group flex gap-3 px-5 hover:bg-bg-hover/50 ${first ? 'pt-2 pb-1' : 'py-0.5'}`}>
                {first ? <Avatar who={m.who} /> : <span className="w-8 shrink-0" aria-hidden />}
                <div className="min-w-0 flex-1">
                  {first && <div className="text-[13.5px] font-bold leading-tight">{m.who === 'you' ? 'You' : 'Conductor'}</div>}
                  {m.who === 'conductor'
                    ? <div data-da="md" className="mt-0.5 text-[14px] leading-relaxed"><ReactMarkdown remarkPlugins={[remarkGfm]}>{m.text}</ReactMarkdown></div>
                    : <div className="mt-0.5 whitespace-pre-wrap text-[14px] leading-relaxed">{m.text}</div>}
                </div>
              </li>
            )
          })}
        </ol>

        {pending && (
          <div className="flex items-center gap-3 px-5 pt-3" data-testid="doc-thread-pending">
            <Avatar who="conductor" />
            <span className="flex items-center gap-1 text-[13px] text-muted">
              Conductor is working on it
              <span className="inline-flex gap-0.5" aria-hidden>
                {[0, 1, 2].map(n => <span key={n} className="h-1 w-1 animate-pulse rounded-full bg-muted" style={{ animationDelay: `${n * 200}ms` }} />)}
              </span>
            </span>
          </div>
        )}

        {workers.length > 0 && (
          <div className="mx-5 mt-4 rounded-lg border border-border">
            <div className="border-b border-border px-3 py-1.5 text-[12px] font-medium text-muted">Workers</div>
            {workers.map(w => (
              <Btn key={w.key} type="button" onClick={() => onOpenSession(w.key)} data-testid="doc-thread-worker"
                className="w-full justify-start rounded-none border-none px-3 py-2 text-[13px] hover:bg-bg-hover">
                <span data-da="dot" data-state={w.state} aria-hidden />
                <span className="min-w-0 flex-1 truncate text-left">{w.title}</span>
                <span className="text-[12px] text-muted">{WORKER_LABEL[w.state]}</span>
              </Btn>
            ))}
          </div>
        )}
      </div>

      <div className="px-4 pb-4 pt-2">
        <div className="rounded-xl border border-border-strong bg-bg focus-within:border-accent focus-within:shadow-[0_0_0_3px_color-mix(in_srgb,var(--accent)_15%,transparent)]">
          <textarea
            ref={inputRef}
            data-testid="doc-thread-input"
            aria-label="Reply in the thread"
            rows={2}
            placeholder="Reply…"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send() }
              else if (e.key === 'Escape') { e.preventDefault(); onClose() }
            }}
            className="block w-full resize-none rounded-t-xl border-none bg-transparent px-3 pt-2.5 text-[14px] text-text outline-none placeholder:text-muted"
          />
          <div className="flex items-center justify-between px-2 pb-2">
            <span className="pl-1 text-[11px] text-muted">Enter to send, Shift+Enter for a new line</span>
            <Btn type="button" onClick={send} disabled={!draft.trim()} aria-label="Send the reply" data-testid="doc-thread-send"
              className={`h-7 w-7 justify-center rounded-md border-none p-0 ${draft.trim() ? 'bg-accent text-accent-fg hover:bg-accent-hover' : 'text-muted'}`}>
              <ArrowUp size={16} />
            </Btn>
          </div>
        </div>
      </div>
    </aside>
  )
}
