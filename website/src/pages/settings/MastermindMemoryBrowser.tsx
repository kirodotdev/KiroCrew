import { useId, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight } from 'lucide-react'

import { api, type MastermindViewLine } from '../../api/client'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { MASTERMIND_VIEW_QUERY_KEY } from '../../hooks/useMastermind'
import { errMessage } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'

/**
 * The memory browser on Settings > Mastermind: the memory view the agent reads
 * each turn, oldest first, one row per line (`id+n | text`). A line that
 * summarizes several messages (n > 1) opens to the two lines under it, and so
 * on down to a single message (n = 1), which opens to that message's full text
 * with "Load more" while the server says there is more (`zoom with at=N`).
 *
 * Every zoom is its own query keyed by `id+n` (and the page offset), so a line
 * opened, closed and opened again does not re-fetch inside the stale window,
 * and a clear on this page invalidates them all with the view's key prefix.
 */

/** The next page offset a single message's text announces, if any. */
const MORE_RE = /zoom with at=(\d+)/

/** `id+n|text` lines (the `browse` reply for n > 1) as view lines. */
export function parseBrowseLines(text: string): MastermindViewLine[] {
  const out: MastermindViewLine[] = []
  for (const raw of text.split('\n')) {
    const m = /^(\d+)\+(\d+)\|(.*)$/.exec(raw)
    if (!m) continue
    const id = Number(m[1])
    const n = Number(m[2])
    out.push({ id, n, ref: `${id}+${n}`, text: m[3], built: true })
  }
  return out
}

export function MastermindMemoryBrowser({ lines }: { lines: MastermindViewLine[] }) {
  if (lines.length === 0) {
    return <p className="text-[13px] text-muted m-0" data-testid="mastermind-browser-empty">{i18nT('pages.settings.mastermindPanel.browser_empty')}</p>
  }
  return (
    <ul className="list-none m-0 p-0 flex flex-col gap-0.5" data-testid="mastermind-browser">
      {lines.map(line => <MemoryLine key={line.ref} line={line} depth={0} />)}
    </ul>
  )
}

function MemoryLine({ line, depth }: { line: MastermindViewLine; depth: number }) {
  const [open, setOpen] = useState(false)
  const regionId = useId()
  // An unbuilt line is a placeholder the summarizer has not written yet: there is
  // nothing to zoom into under it, except a single message, which is its own text.
  const expandable = line.built || line.n === 1
  const label = open
    ? i18nT('pages.settings.mastermindPanel.collapse_line', { ref: line.ref })
    : i18nT('pages.settings.mastermindPanel.expand_line', { ref: line.ref })
  const body = (
    <>
      <span className="font-mono text-[12px] text-muted shrink-0 tabular-nums">{line.ref}</span>
      <span className="text-muted shrink-0" aria-hidden="true">|</span>
      <span className={`min-w-0 break-words ${line.built ? 'text-text' : 'text-muted italic'}`}>
        {line.built ? line.text : i18nT('pages.settings.mastermindPanel.not_summarized')}
      </span>
    </>
  )
  return (
    <li data-testid={`mastermind-line-${line.ref}`} data-built={line.built ? 'true' : 'false'}>
      {expandable ? (
        <button type="button"
          className="w-full flex items-start gap-1.5 text-left text-[13px] bg-transparent border-none rounded px-1 py-0.5 cursor-pointer hover:bg-bg-hover focus-ring"
          aria-expanded={open} aria-controls={regionId} aria-label={label} title={label}
          onClick={() => setOpen(v => !v)}>
          <ChevronRight size={12} aria-hidden="true" className={`mt-[3px] shrink-0 text-muted transition-transform ${open ? 'rotate-90' : ''}`} />
          {body}
        </button>
      ) : (
        <div className="flex items-start gap-1.5 text-[13px] px-1 py-0.5 pl-[22px]">{body}</div>
      )}
      {expandable && (
        <div id={regionId} hidden={!open} className="pl-4 border-l border-border ml-2">
          {open && (line.n === 1
            ? <MessageText id={line.id} />
            : <ChildLines id={line.id} n={line.n} depth={depth + 1} />)}
        </div>
      )}
    </li>
  )
}

function ChildLines({ id, n, depth }: { id: number; n: number; depth: number }) {
  const { data, error, isPending } = useQuery({
    queryKey: [...MASTERMIND_VIEW_QUERY_KEY, 'browse', id, n],
    queryFn: () => api.mastermindBrowse(id, n),
    staleTime: 30_000,
  })
  if (isPending) return <p className="text-[12px] text-muted m-0 px-1 py-0.5">{i18nT('pages.settings.mastermindPanel.loading')}</p>
  if (error) {
    return (
      <ErrorNotice variant="inline" title={i18nT('pages.settings.mastermindPanel.browse_failed')} message={errMessage(error)}
        askAgent testId={`mastermind-browse-error-${id}+${n}`} />
    )
  }
  const children = parseBrowseLines(data?.text ?? '')
  return (
    <ul className="list-none m-0 p-0 flex flex-col gap-0.5" data-testid={`mastermind-children-${id}+${n}`}>
      {children.map(child => <MemoryLine key={child.ref} line={child} depth={depth} />)}
    </ul>
  )
}

/** One message's full text, paged: each "Load more" fetches from the offset the
 *  previous page announced and appends it. */
function MessageText({ id }: { id: number }) {
  const [pages, setPages] = useState<number[]>([0])
  return (
    <div className="flex flex-col gap-1 px-1 py-0.5" data-testid={`mastermind-message-${id}`}>
      {pages.map((at, i) => (
        <MessagePage key={at} id={id} at={at} last={i === pages.length - 1}
          onMore={next => setPages(prev => (prev.includes(next) ? prev : [...prev, next]))} />
      ))}
    </div>
  )
}

function MessagePage({ id, at, last, onMore }: { id: number; at: number; last: boolean; onMore: (at: number) => void }) {
  const { data, error, isPending } = useQuery({
    queryKey: [...MASTERMIND_VIEW_QUERY_KEY, 'browse', id, 1, at],
    queryFn: () => api.mastermindBrowse(id, 1, at),
    staleTime: 30_000,
  })
  if (isPending) return <p className="text-[12px] text-muted m-0">{i18nT('pages.settings.mastermindPanel.loading')}</p>
  if (error) {
    return (
      <ErrorNotice variant="inline" title={i18nT('pages.settings.mastermindPanel.browse_failed')} message={errMessage(error)}
        askAgent testId={`mastermind-browse-error-${id}+1@${at}`} />
    )
  }
  const text = data?.text ?? ''
  const more = MORE_RE.exec(text)
  const next = more ? Number(more[1]) : null
  return (
    <>
      <p className="text-[13px] text-text m-0 whitespace-pre-wrap break-words">{text}</p>
      {last && next !== null && next > at && (
        <div>
          <Btn onClick={() => onMore(next)} data-testid={`mastermind-load-more-${id}`}>
            {i18nT('pages.settings.mastermindPanel.load_more')}
          </Btn>
        </div>
      )}
    </>
  )
}
