/** The folded Slack code channel crew's three marks on its conductor's session
 *  row (`codeChannelCrew.ts` derives what they say): the status line that
 *  speaks for the hidden workers, the fold toggle with their count, and the
 *  most pressing thing a folded worker wants. `SessionRow` places them; their
 *  markup lives here so the row file stays one readable unit. */
import { ShieldCheck, CircleCheck, Loader2, TriangleAlert } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { DisclosureChevron } from './chrome'
import type { CodeChannelCrewView, CrewAttention, CrewState } from './codeChannelCrew'
import { CodeChannelBadge } from '../../components/CodeChannelBadge'
import type { CodeChannelInfo } from '../../types'

/** The conductor's status line for its folded crew. `lineCls` and `iconPx` are
 *  the row's own status-line class and glyph size, so this line matches the
 *  lines around it. */
export function CodeChannelCrewLine({ state: st, lineCls, iconPx }: { state: CrewState; lineCls: string; iconPx: number }) {
  const withDetail = (text: string) => (st.detail ? `${text} · ${st.detail}` : text)
  if (st.key === 'needs_you') {
    const title = withDetail(i18nT('pages.chatSidebar.code_channel_crew_waiting_title', { count: st.count }))
    // A worker's tool approval reads as its own row reads it.
    const word = i18nT(st.approval ? 'pages.chatSidebar.needs_approval' : 'pages.chatSidebar.code_channel_crew_waiting')
    return (
      <div className={lineCls} title={title} data-testid="code-channel-crew-state" data-crew-state={st.key}>
        <ShieldCheck size={iconPx} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
        <span className="truncate"><span className="font-medium" style={{ color: 'var(--warn)' }}>{word}</span>{st.detail ? <span className="text-muted"> · {st.detail}</span> : null}</span>
      </div>
    )
  }
  // The other states: a glyph, the state in words, and the trailing
  // detail (the aggregate's label) in muted ink, like the lines above.
  const detail = st.detail ? <span className="text-muted"> · {st.detail}</span> : null
  if (st.key === 'interrupted' || st.key === 'stuck') {
    const label = i18nT(st.key === 'stuck' ? 'pages.chatSidebar.code_channel_crew_stuck' : 'pages.chatSidebar.code_channel_crew_interrupted', { count: st.count })
    return (
      <div className={lineCls} title={withDetail(label)} data-testid="code-channel-crew-state" data-crew-state={st.key}>
        <TriangleAlert size={iconPx} className="shrink-0 text-danger" aria-hidden />
        <span className="truncate"><span className="font-medium text-danger">{label}</span>{detail}</span>
      </div>
    )
  }
  if (st.key === 'done') {
    const label = st.total != null && st.total > 0
      ? i18nT('pages.chatSidebar.code_channel_crew_done', { count: st.count, total: st.total })
      : i18nT('pages.chatSidebar.code_channel_crew_done_plain')
    return (
      <div className={lineCls} title={withDetail(label)} data-testid="code-channel-crew-state" data-crew-state={st.key}>
        <CircleCheck size={iconPx} className="shrink-0 text-ok" aria-hidden />
        <span className="truncate"><span className="font-medium text-ok">{label}</span>{detail}</span>
      </div>
    )
  }
  const label = i18nT('pages.chatSidebar.crew_attention_working', { count: st.count })
  return (
    <div className={lineCls} title={withDetail(label)} data-testid="code-channel-crew-state" data-crew-state={st.key}>
      <Loader2 size={iconPx} className="shrink-0 text-accent animate-spin motion-reduce:animate-none" aria-hidden />
      <span className="truncate"><span className="font-medium text-accent">{label}</span>{detail}</span>
    </div>
  )
}

/** The fold for a code channel's workers (folder view only): a chevron and
 *  their count, beside the channel's mark. Its events stop at the button, so a
 *  press never selects or drags the row. */
export function CodeChannelCrewToggle({ workers, expanded, onToggle }: { workers: number; expanded: boolean; onToggle: () => void }) {
  const label = i18nT(expanded ? 'pages.chatSidebar.code_channel_crew_hide' : 'pages.chatSidebar.code_channel_crew_show', { count: workers })
  return (
    <button
      type="button"
      className="inline-flex shrink-0 items-center gap-0.5 rounded-sm px-0.5 text-muted hover:text-text bg-transparent border-none cursor-pointer tabular-nums focus-ring"
      onMouseDown={e => e.stopPropagation()}
      onPointerDown={e => e.stopPropagation()}
      onClick={e => { e.stopPropagation(); onToggle() }}
      onAuxClick={e => e.stopPropagation()}
      aria-expanded={expanded}
      aria-label={label}
      title={label}
      data-testid="code-channel-crew-toggle"
    >
      <DisclosureChevron open={expanded} size={10} />
      <span aria-hidden="true">{workers}</span>
    </button>
  )
}

/** The most pressing thing a folded worker wants: an owed decision (shield), an
 *  interrupted turn (warning triangle), or an unread result (a small ok-coloured
 *  dot, named as an unread row's dot is). Each kind has the shape the crew line
 *  draws for that state, so the kind never rests on colour alone. */
export function CodeChannelCrewAttentionMark({ attention, interrupted, iconPx }: { attention: CrewAttention; interrupted: number; iconPx: number }) {
  const label = attention === 'needs_you'
    ? i18nT('pages.chatSidebar.code_channel_crew_waiting')
    : attention === 'interrupted'
      ? i18nT('pages.chatSidebar.code_channel_crew_interrupted', { count: Math.max(1, interrupted) })
      : i18nT('pages.chatSidebar.agent_finished_your_turn')
  const testProps = { role: 'img', 'aria-label': label, title: label, 'data-testid': 'code-channel-crew-attention', 'data-crew-attention': attention }
  if (attention === 'needs_you') {
    return <span className="inline-flex shrink-0" {...testProps}><ShieldCheck size={iconPx} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden /></span>
  }
  if (attention === 'interrupted') {
    return <span className="inline-flex shrink-0" {...testProps}><TriangleAlert size={iconPx} className="shrink-0 text-danger" aria-hidden /></span>
  }
  return <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: 'var(--ok)' }} {...testProps} />
}


/** A code-channel session's marks in its row's meta line: the channel badge
 *  (the Slack mark, a link to the channel in a fixed box that never shrinks,
 *  its name in the link's accessible name and tooltip, plus an archived or
 *  finished state in words), then, on a conductor in the folder view, the fold
 *  for its workers and a folded worker's most pressing want, so folding never
 *  hides it. An unread want is skipped while the row shows its own unread dot,
 *  so the row never shows two. Renders nothing for any other session. */
export function CodeChannelRowMarks({ info, crew, isUnread, iconPx, onToggle }: {
  info?: CodeChannelInfo
  crew?: CodeChannelCrewView | null
  isUnread: boolean
  iconPx: number
  onToggle: () => void
}) {
  return (
    <>
      {info && <CodeChannelBadge info={info} variant="row" />}
      {crew && crew.workers > 0 && <CodeChannelCrewToggle workers={crew.workers} expanded={crew.expanded} onToggle={onToggle} />}
      {crew?.attention && !(crew.attention === 'unread' && isUnread) && <CodeChannelCrewAttentionMark attention={crew.attention} interrupted={crew.interrupted} iconPx={iconPx} />}
    </>
  )
}
