import {
  forwardRef,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type HTMLAttributes,
} from 'react'
import {
  Virtuoso,
  type Components,
  type ContextProp,
  type VirtuosoHandle,
} from 'react-virtuoso'
import { AlertTriangle, ArrowDownToLine, MessageSquareText, ScrollText } from 'lucide-react'

import { Btn } from '../../../components/ui'
import { fmtTime } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import type { MeetingStatus, TranscriptSegment } from '../api'

const FOLLOW_THRESHOLD_PX = 48
const VIRTUALIZE_AFTER_SEGMENTS = 200

interface VirtualContext {
  partial: string
  primary: boolean
}

const VirtualItem = forwardRef<
  HTMLDivElement,
  HTMLAttributes<HTMLDivElement> & ContextProp<VirtualContext>
>(function VirtualItem({ context: _context, ...props }, ref) {
  const className = `${props.className ?? ''} pb-3`.trim()
  return <div {...props} ref={ref} role="listitem" className={className} />
})

function VirtualFooter({ context }: ContextProp<VirtualContext>) {
  return context.partial ? <PartialRow text={context.partial} className="pb-3" /> : null
}

const VirtualList = forwardRef<
  HTMLDivElement,
  HTMLAttributes<HTMLDivElement> & ContextProp<VirtualContext>
>(function VirtualList({ context, ...props }, ref) {
  const className = `${props.className ?? ''} ${
    context.primary ? 'w-full max-w-3xl mx-auto' : ''
  }`.trim()
  return <div {...props} ref={ref} role="list" className={className} />
})

const VIRTUAL_COMPONENTS: Components<TranscriptSegment, VirtualContext> = {
  Footer: VirtualFooter,
  Item: VirtualItem,
  List: VirtualList,
}

interface Props {
  segments: TranscriptSegment[]
  partial?: string
  primary?: boolean
  status?: MeetingStatus
  full?: boolean
}

interface RowProps {
  segment: TranscriptSegment
}

function TranscriptRow({ segment }: RowProps) {
  const typed = segment.source === 'typed'
  return (
    <div className="grid grid-cols-[auto_minmax(0,1fr)] gap-2.5">
      <time
        dateTime={segment.timestamp}
        className="text-[11px] tabular-nums text-muted pt-0.5"
      >
        {fmtTime(segment.timestamp)}
      </time>
      <div className="min-w-0">
        {typed ? (
          <span className="inline-flex items-center gap-1 text-[11px] text-muted mb-0.5">
            <MessageSquareText className="lucide-inline" />
            {i18nT('apps.meetings.transcript.typed')}
          </span>
        ) : null}
        <p className="text-[13px] leading-5 text-text whitespace-pre-wrap break-words">
          {segment.text}
        </p>
      </div>
    </div>
  )
}

function PartialRow({ text, className = '' }: { text: string; className?: string }) {
  return (
    <div
      className={`grid grid-cols-[auto_minmax(0,1fr)] gap-2.5 text-muted ${className}`}
    >
      <span className="text-[11px] pt-0.5">
        {i18nT('apps.meetings.transcript.live')}
      </span>
      <p className="text-[13px] leading-5 italic break-words">{text}</p>
    </div>
  )
}

export default function TranscriptPanel({
  segments,
  partial = '',
  primary = false,
  status = 'active',
  full = false,
}: Props) {
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const virtuosoRef = useRef<VirtuosoHandle | null>(null)
  const [following, setFollowing] = useState(true)
  const virtualized = segments.length > VIRTUALIZE_AFTER_SEGMENTS
  const visiblePartial = full ? '' : partial
  const virtualContext = useMemo(
    () => ({ partial: visiblePartial, primary }),
    [primary, visiblePartial],
  )
  const latestDurableSegment = segments.at(-1)

  const scrollToLatest = useCallback((behavior: ScrollBehavior = 'smooth') => {
    if (virtualized) {
      virtuosoRef.current?.scrollTo({ top: Number.MAX_SAFE_INTEGER, behavior })
      return
    }
    const scroller = scrollerRef.current
    if (!scroller) return
    scroller.scrollTo({ top: scroller.scrollHeight, behavior })
  }, [virtualized])

  useEffect(() => {
    if (!following) return
    const frame = window.requestAnimationFrame(() => scrollToLatest('auto'))
    return () => window.cancelAnimationFrame(frame)
  }, [following, scrollToLatest, segments.length, visiblePartial])

  const onScroll = () => {
    const scroller = scrollerRef.current
    if (!scroller) return
    const distance = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight
    setFollowing(distance <= FOLLOW_THRESHOLD_PX)
  }

  const liveEmptyState = status === 'active' && !full

  return (
    <section
      aria-label={i18nT('apps.meetings.transcript.regionLabel')}
      className="relative h-full min-h-0 rounded-xl border border-border bg-card overflow-hidden flex flex-col shadow-sm"
    >
      <p className="sr-only" aria-live="polite" aria-atomic="true">
        {latestDurableSegment?.text ?? ''}
      </p>
      <header className="flex-none flex items-center gap-2.5 px-4 py-3 border-b border-border bg-bg-elevated/40">
        <ScrollText className="lucide-inline text-accent" />
        <h3 className="text-sm font-semibold text-text-strong">
          {i18nT('apps.meetings.transcript.title')}
        </h3>
      </header>

      {full ? (
        <div
          role="status"
          className="flex-none mx-4 mt-3 rounded-lg border border-danger/20 bg-danger/10 px-3 py-2 text-[12px] text-danger flex items-start gap-2"
        >
          <AlertTriangle className="lucide-inline flex-none mt-0.5" />
          <span>{i18nT('apps.meetings.transcript.full')}</span>
        </div>
      ) : null}

      {primary ? (
        <div className="flex-none mx-4 mt-3 rounded-lg border border-border bg-bg-elevated/40 px-3 py-2">
          <p className="text-[12px] font-medium text-text-strong">
            {i18nT('apps.meetings.meeting.noAgents')}
          </p>
          <p className="text-[12px] text-muted mt-0.5">
            {i18nT('apps.meetings.meeting.noAgentsHint')}
          </p>
        </div>
      ) : null}

      {segments.length === 0 && !visiblePartial ? (
        <div className="flex-1 min-h-[180px] flex flex-col items-center justify-center text-center px-5">
          <ScrollText className="lucide-inline text-muted mb-3" />
          <p className="text-sm font-medium text-text-strong">
            {i18nT('apps.meetings.transcript.empty')}
          </p>
          <p className="text-[13px] text-muted mt-1 max-w-sm">
            {i18nT(
              liveEmptyState
                ? 'apps.meetings.transcript.emptyHintLive'
                : 'apps.meetings.transcript.emptyHintRecorded',
            )}
          </p>
        </div>
      ) : virtualized ? (
        <Virtuoso
          ref={virtuosoRef}
          className="flex-1 min-h-0 px-4 pt-3"
          data={segments}
          context={virtualContext}
          components={VIRTUAL_COMPONENTS}
          computeItemKey={(_index, segment) => segment.id}
          itemContent={(_index, segment) => <TranscriptRow segment={segment} />}
          atBottomThreshold={FOLLOW_THRESHOLD_PX}
          atBottomStateChange={setFollowing}
          followOutput={following ? 'auto' : false}
        />
      ) : (
        <div
          ref={scrollerRef}
          onScroll={onScroll}
          className="flex-1 min-h-0 overflow-y-auto px-4 py-3"
        >
          <ol className={`flex flex-col gap-3 ${primary ? 'w-full max-w-3xl mx-auto' : ''}`}>
            {segments.map(segment => (
              <li key={segment.id}>
                <TranscriptRow segment={segment} />
              </li>
            ))}
            {visiblePartial ? (
              <li>
                <PartialRow text={visiblePartial} />
              </li>
            ) : null}
          </ol>
        </div>
      )}

      {!following ? (
        <Btn
          className="absolute right-4 bottom-4 shadow-md"
          onClick={() => {
            setFollowing(true)
            scrollToLatest()
          }}
        >
          <ArrowDownToLine className="lucide-inline" />
          {i18nT('apps.meetings.transcript.jumpToLatest')}
        </Btn>
      ) : null}
    </section>
  )
}
