// ThreadReplies + ReplyMarker — how a forge thread reads on the timeline.
//
// ThreadReplies draws a thread's replies indented under its root card, oldest
// first. Back-to-back replies by one author share a card (see groupRuns). A long
// thread shows its newest few and folds the rest behind "Show N earlier replies". ReplyMarker is the compact row a reply leaves at its own place
// in time (see lib/threads); clicking it reveals the reply in its thread, unfolding
// it if needed, scrolls there and briefly highlights it.

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ChevronDown, CornerDownRight } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import type { TimelineEvent } from '../api'
import { relativeTimeOrDate } from '../lib/format'
import { REPLY_ATTR, groupRuns, replyDomKey } from '../lib/threads'

const REVEAL_EVENT = 'issue-radar:reveal-reply'
/** Replies shown before the rest fold away. */
const VISIBLE_TAIL = 3
// Inset: a reply sits inside its run's clipped frame, where an outer ring is cut.
const FLASH = ['ring-2', 'ring-inset', 'ring-accent', 'rounded-lg']

function flash(el: Element) {
  el.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
  el.classList.add(...FLASH)
  window.setTimeout(() => el.classList.remove(...FLASH), 1600)
}

function findReply(root: ParentNode, key: string): Element | null {
  for (const el of root.querySelectorAll(`[${REPLY_ATTR}]`)) {
    if (el.getAttribute(REPLY_ATTR) === key) return el
  }
  return null
}

/** How a reply sits in its run: the first carries the header; a later one shows
 * its time only when it reads differently from the reply before it, so a run
 * posted together carries no repeated "9 days ago" rows. */
export type ReplyPart = { continuation: boolean; showTime: boolean }

export function ThreadReplies({
  replies, renderReply,
}: {
  replies: TimelineEvent[]
  /** One reply's content, WITHOUT a card frame: the run's frame is drawn here. */
  renderReply: (ev: TimelineEvent, part: ReplyPart) => ReactNode
}) {
  const [expanded, setExpanded] = useState(false)
  // The reply a marker asked for; flashed once the unfold has rendered.
  const [reveal, setReveal] = useState<string | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const runs = useMemo(() => groupRuns(replies), [replies])
  // Fold whole runs, never half of one: keep the newest runs until they hold at
  // least VISIBLE_TAIL replies. The toggle still counts replies, not runs.
  const firstShown = useMemo(() => {
    if (expanded) return 0
    let shown = 0
    let i = runs.length
    while (i > 0 && shown < VISIBLE_TAIL) shown += runs[--i].length
    return i
  }, [runs, expanded])
  const hidden = runs.slice(0, firstShown).reduce((n, run) => n + run.length, 0)

  useEffect(() => {
    const keys = new Set(replies.map(replyDomKey))
    const onReveal = (e: Event) => {
      const key = (e as CustomEvent<string>).detail
      if (!keys.has(key)) return
      setExpanded(true)
      setReveal(key)
    }
    window.addEventListener(REVEAL_EVENT, onReveal)
    return () => window.removeEventListener(REVEAL_EVENT, onReveal)
  }, [replies])

  useEffect(() => {
    if (!reveal) return
    const el = rootRef.current && findReply(rootRef.current, reveal)
    if (el) flash(el)
    setReveal(null)
  }, [reveal, expanded])

  return (
    <div ref={rootRef} className="mt-2 ml-4 space-y-2 border-l-2 border-border pl-3" data-testid="thread-replies">
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text focus-ring rounded px-1"
        >
          <ChevronDown size={12} />
          {i18nT('apps.issueRadar.components.threads.show_earlier', { count: hidden })}
        </button>
      )}
      {runs.slice(firstShown).map((run) => (
        <div
          key={replyDomKey(run[0])}
          className="rounded-lg border border-border bg-card overflow-hidden"
          data-testid="reply-run"
        >
          {/* Each message keeps its own key, so a marker flashes that message. */}
          {run.map((ev, j) => (
            <div key={replyDomKey(ev)} data-thread-reply={replyDomKey(ev)} className="transition-shadow">
              {renderReply(ev, {
                continuation: j > 0,
                showTime: j === 0 || relativeTimeOrDate(ev.created_at) !== relativeTimeOrDate(run[j - 1].created_at),
              })}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

/** First line of a reply, for its marker. */
function firstLine(body: string | undefined): string {
  const line = (body ?? '').trim().split('\n').find((l) => l.trim() && !l.startsWith('>')) ?? ''
  return line.length > 90 ? `${line.slice(0, 89)}…` : line
}

export function ReplyMarker({ ev, root }: { ev: TimelineEvent; root: TimelineEvent }) {
  const label = i18nT('apps.issueRadar.components.threads.replied_in_thread', {
    author: ev.actor ?? 'ghost', root: root.actor ?? 'ghost',
  })
  return (
    <button
      type="button"
      onClick={() => window.dispatchEvent(new CustomEvent(REVEAL_EVENT, { detail: replyDomKey(ev) }))}
      title={i18nT('apps.issueRadar.components.threads.jump_to_reply')}
      className="group flex w-full min-w-0 items-center gap-1.5 pt-1 text-left text-[12.5px] text-muted hover:text-text focus-ring rounded"
      data-testid="reply-marker"
    >
      <CornerDownRight size={12} className="shrink-0" />
      {/* The label names whose thread it is, so it is never cut: in a narrow
          pane it wraps (long handles break), and only the preview truncates. */}
      <span className="min-w-0 [overflow-wrap:anywhere]">
        {label}{firstLine(ev.body) ? ':' : ''}
      </span>
      {firstLine(ev.body) && (
        <span className="min-w-0 flex-1 truncate opacity-80 group-hover:opacity-100">{firstLine(ev.body)}</span>
      )}
    </button>
  )
}
