import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueries, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ChevronDown, PenLine, Square, Users } from 'lucide-react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { api, type CrewTeam, type MemberActivityEntry, type MemberRosterRow } from '../../api/client'
import { memberActivityQueryKey, membersRosterQuery } from '../../api/membersQuery'
import CrewAvatar from '../../components/CrewAvatar'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, ContentSkeleton } from '../../components/ui'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import { useConnected } from '../../hooks/useConnected'
import { useMemberProjection } from '../../state/useMemberProjection'
import type { ActivityView } from '../../state/memberProjectionTypes'
import { timeAgo } from '../../utils/timeAgo'
import { mergePaneDraft } from '../../utils/chatPaneDrafts'
import { cn } from '../../lib/utils'
import type { ChatMessage } from '../../types'
import { projectLabel } from './activityDays'
import {
  WEEK_MAX_ITEMS,
  WEEK_SECONDS,
  newestQuestionFirst,
  teamMemberState,
  unansweredQuestion,
  type OpenQuestion,
  type TeamMemberState,
} from './teamGroups'

/** Rows read from the tail of each crewmate's chat when looking for an
 *  unanswered question. The question, if any, is the newest conversational
 *  row, so a short tail is enough; the page never renders the transcript. */
const CHAT_TAIL_LIMIT = 30

/** How long a chat tail is trusted before the view re-reads it on its own.
 *  Live slot frames (a turn starting or ending, a question flag flipping)
 *  invalidate it sooner -- see the `liveVersion` effect. */
const CHAT_TAIL_STALE_MS = 15_000

const chatTailQueryKey = (slotKey: string) => ['team-chat-tail', slotKey] as const

type TFn = ReturnType<typeof useTranslation>['t']

/** Where a crewmate's confirmed thread key lives while the team view is open.
 *  Written by the thread endpoint's answer, the one creator/repairer of member
 *  slots, never by the roster binding. Keyed by the GATEWAY CONNECTION the
 *  answer was given under as well: a dropped-then-restored socket is the one
 *  client-visible sign the gateway may have restarted and handed the slot
 *  behind a key to someone else, so an answer from before the drop is a
 *  different entry -- absent, not stale -- until this connection answers. */
let nextOpenGeneration = 0

const memberThreadQueryKey = (slug: string, openGeneration: number, connection: number) =>
  ['team-member-thread', slug, openGeneration, connection] as const

/** What the page passes for each crewmate: the roster row plus the live
 *  readings the page already resolves for its roster rows. */
export interface TeamMemberInput {
  row: MemberRosterRow
  /** The key the page resolves the row's LIVE readings (presence, needs-you)
   *  to. NOT trusted as a chat to read or draft into: the roster binding
   *  outlives the live slot and can resolve to a foreign one -- the tail read
   *  and the draft below use the thread endpoint's confirmed key instead. */
  slotKey: string
  running: boolean
  /** The slot frame says the turn is parked on the user (approval / question). */
  needsInput: boolean
  /** Withhold slug-keyed projections: two rows share this slug. */
  slugCollides: boolean
}

interface WeekItem {
  row: MemberRosterRow
  entry: MemberActivityEntry
}

interface InboxItem {
  member: TeamMemberInput
  /** The parsed question, or `null` when the live frame says the turn waits on
   *  the user (an approval, a question the tail does not show) but the tail
   *  holds no question to quote: the card then says so and offers the chat. */
  question: OpenQuestion | null
}

function localMidnightEpoch(now: number): number {
  const d = new Date(now * 1000)
  d.setHours(0, 0, 0, 0)
  return Math.floor(d.getTime() / 1000)
}

/**
 * The team view: the manager's desk for one team, shown in the main pane where
 * a crewmate's chat would be. Three blocks, all scoped to the team's crewmates
 * and all DERIVED from what those crewmates already have -- their threads,
 * their activity records, their live presence. A team stores nothing of its
 * own beyond a name and a member list.
 */
export default function TeamView({
  team,
  members,
  rosterFailed = false,
  onOpenMember,
  onEdit,
  onBack,
}: {
  team: CrewTeam
  /** The team's crewmates as the roster shows them (roster order), with live readings. */
  members: readonly TeamMemberInput[]
  /** A missing roster is an error, never proof the team is empty. */
  rosterFailed?: boolean
  onOpenMember: (row: MemberRosterRow) => void
  onEdit: () => void
  /** Below md the roster is the page; this returns to it. */
  onBack: () => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const reduceMotion = useReducedMotion()

  // Activity per crewmate: the same entries (and the same query key) the
  // crewmate's own drawer reads, so a drawer visit and this view share one
  // fetch. Feeds both the "This week" list and the status strip's counts.
  const activityQueries = useQueries({
    queries: members.map((m) => ({
      queryKey: memberActivityQueryKey(m.row.slug, m.row.name),
      queryFn: () => api.memberActivity(m.row.slug, m.row.name),
      staleTime: membersRosterQuery.staleTime,
    })),
  })

  // Which chat is a crewmate's: asked of the thread endpoint, never read off
  // the roster's `slot_key`. dm.json outlives the live slot, so an unconfirmed
  // binding can name a slot that is no longer (or never was) this crewmate's
  // pinned thread; reading its tail or drafting into it would attribute a
  // foreign transcript to the crewmate. POST /api/members/{slug}/thread is the
  // idempotent creator/repairer the page itself opens threads through; only a
  // crewmate whose roster row carries a `slot_key` -- the server writes one
  // exactly for a bound crewmate, and never serializes a `bound` flag -- is
  // asked, so a never-messaged crewmate gets no slot made on its behalf. An
  // answer naming another crewmate (a slug collision) is discarded.
  const bound = useMemo(() => members.filter((m) => !!m.row.slot_key), [members])
  // Which connection the confirmations belong to. Counts true->false->true
  // sequences seen by THIS mounted view, as the member page does for its own
  // re-POST: the first connect after a reload is not a reconnect, and the
  // open confirms every thread then. Each reconnect re-keys the queries
  // below, so a key confirmed BEFORE the drop stops feeding the tails and
  // the drafts at once -- not after a refetch that may fail and leave it in
  // place -- and nothing here reads or writes a slot until the gateway that
  // is serving NOW has said which slot is the crewmate's.
  //
  // The DROP itself retires them too, not only the reconnect that follows:
  // between the two the gateway may already be a restarted one that has
  // handed the slot to someone else, and a card still on screen would let an
  // answer -- a draft, a message -- go into that other session. `dropped`
  // holds from the moment the connection is lost until the reconnect has
  // re-keyed the queries; a first load that has not connected yet is not a
  // drop, its confirmations were answered by the gateway serving now.
  const connected = useConnected()
  // A team close/reopen is a fresh confirmation boundary too. React Query keeps
  // earlier answers after unmount, so a mount-scoped generation prevents a
  // prior opening's slot from feeding tails or drafts before this opening's
  // POST has answered.
  const [openGeneration] = useState(() => ++nextOpenGeneration)
  const hadConnectionRef = useRef(false)
  const [connection, setConnection] = useState(0)
  const [dropped, setDropped] = useState(false)
  const confirmationsRetired = dropped || (hadConnectionRef.current && !connected)
  useEffect(() => {
    if (!connected) {
      if (hadConnectionRef.current) setDropped(true)
      return
    }
    if (hadConnectionRef.current) setConnection((n) => n + 1)
    hadConnectionRef.current = true
    setDropped(false)
  }, [connected])
  // The endpoint is a WRITE (membersQuery.ts: the idempotent creator /
  // repairer of member slots, mutation-only on the member page), so it is
  // asked exactly once per team OPEN -- `refetchOnMount: 'always'` is that
  // open, as `openThread`'s re-POST is on the member page -- and once more
  // per gateway reconnect (the key above), the one moment the gateway may
  // have restarted under the slot; `refetchOnReconnect` is the browser's own
  // offline->online edge, the other such moment. Never on its own: no stale
  // window, no focus refetch, no retry. A finite staleTime under the client's
  // default focus refetch had turned it into a cached read that re-issued one
  // owner write per bound crewmate on every window focus.
  const threadQueries = useQueries({
    queries: bound.map((m) => ({
      queryKey: memberThreadQueryKey(m.row.slug, openGeneration, connection),
      queryFn: () => api.memberThread(m.row.slug),
      staleTime: Infinity,
      refetchOnMount: 'always' as const,
      refetchOnWindowFocus: false,
      refetchOnReconnect: 'always' as const,
      retry: false,
    })),
  })
  const confirmedKey = useMemo(() => {
    const out = new Map<string, string>()
    if (confirmationsRetired) return out
    bound.forEach((m, i) => {
      const d = threadQueries[i]?.data
      if (d && d.member === m.row.name && d.slot_key) out.set(m.row.name, d.slot_key)
    })
    return out
  }, [bound, threadQueries, confirmationsRetired])
  const threadsLoading = threadQueries.some((q) => q.data === undefined && !q.isError)
  // Failure is failure whether or not a cached answer is still on screen: the
  // notice says the inbox / week may be stale, the cached rows stay readable.
  const threadsFailed = threadQueries.some((q) => q.isError)

  // The tail of each crewmate's chat, for the questions still waiting on the
  // user. Only crewmates with a CONFIRMED thread have a chat to read; one that
  // has never been talked to cannot have asked anything.
  const withChat = useMemo(() => members.filter((m) => confirmedKey.has(m.row.name)), [members, confirmedKey])
  const tailQueries = useQueries({
    queries: withChat.map((m) => {
      const key = confirmedKey.get(m.row.name) ?? ''
      return {
        queryKey: chatTailQueryKey(key),
        queryFn: async () => {
          const d = (await api.chatSlotDetail(key, CHAT_TAIL_LIMIT)) as { messages?: ChatMessage[] }
          return unansweredQuestion(d.messages ?? [])
        },
        staleTime: CHAT_TAIL_STALE_MS,
      }
    }),
  })
  // A turn ending or a question flag flipping on a team slot means THAT
  // slot's tail may have changed: re-read it rather than waiting out the
  // stale window -- only it, so one busy crewmate does not refetch the whole
  // team's tails on every flip.
  const liveVersion = members.map((m) => `${m.row.name}:${m.running ? 1 : 0}${m.needsInput ? 1 : 0}`).join('|')
  const seenLive = useRef<Map<string, string>>(new Map())
  useEffect(() => {
    const next = new Map<string, string>()
    for (const m of members) {
      const state = `${m.running ? 1 : 0}${m.needsInput ? 1 : 0}`
      next.set(m.row.name, state)
      const key = confirmedKey.get(m.row.name)
      if (key && seenLive.current.has(m.row.name) && seenLive.current.get(m.row.name) !== state) {
        void queryClient.invalidateQueries({ queryKey: chatTailQueryKey(key) })
      }
    }
    seenLive.current = next
    // liveVersion is the members' live readings folded into one string, the
    // thing this effect actually watches.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveVersion, confirmedKey, queryClient])

  const now = Math.floor(Date.now() / 1000)
  const midnight = localMidnightEpoch(now)
  const weekStart = now - WEEK_SECONDS

  const inbox = useMemo<InboxItem[]>(() => {
    const out: InboxItem[] = []
    withChat.forEach((m, i) => {
      const q = tailQueries[i]?.data
      if (q) out.push({ member: m, question: q })
      // The slot frame says the turn is parked on the user but the tail
      // shows no question (an approval wait, a question the tail cannot
      // parse): the strip reads "waiting", so the inbox must not read
      // "nothing waiting" -- a plain card sends the user to the chat.
      else if (q === null && m.needsInput) out.push({ member: m, question: null })
    })
    // Newest question first; a row without a usable timestamp sinks to the end.
    out.sort(newestQuestionFirst)
    return out
  }, [withChat, tailQueries])
  const inboxLoading = confirmationsRetired || threadsLoading || tailQueries.some((q) => q.data === undefined && !q.isError)
  const inboxFailed = threadsFailed || tailQueries.some((q) => q.isError)

  const week = useMemo<WeekItem[]>(() => {
    const out: WeekItem[] = []
    members.forEach((m, i) => {
      for (const entry of activityQueries[i]?.data?.entries ?? []) {
        if (entry.ts >= weekStart) out.push({ row: m.row, entry })
      }
    })
    out.sort((a, b) => b.entry.ts - a.entry.ts)
    return out.slice(0, WEEK_MAX_ITEMS)
  }, [members, activityQueries, weekStart])
  const weekLoading = activityQueries.some((q) => q.data === undefined && !q.isError)
  const weekFailed = activityQueries.some((q) => q.isError)

  const waitingNames = useMemo(() => {
    const names = new Set<string>()
    for (const m of members) if (m.needsInput) names.add(m.row.name)
    for (const item of inbox) names.add(item.member.row.name)
    return names
  }, [members, inbox])

  const openWithDraft = (row: MemberRosterRow, slotKey: string, text: string) => {
    // The chip PRE-FILLS the crewmate's composer and opens its chat; the user
    // presses send there. Answering from outside the chat would post into a
    // conversation the user is not looking at.
    if (slotKey && text) mergePaneDraft(slotKey, text, [])
    onOpenMember(row)
  }

  return (
    <div className="flex-1 min-h-0 flex flex-col" data-testid="team-view" data-team={team.id}>
      <header className="flex items-center gap-2.5 px-4 py-2 shrink-0" data-testid="team-header">
        <button
          type="button"
          onClick={onBack}
          className="md:hidden inline-flex items-center p-1 -ml-1 rounded hover:bg-accent/40"
          aria-label={t('pages.membersPage.title')}
          data-testid="team-back"
        >
          <ArrowLeft size={16} className="lucide-inline" />
        </button>
        <span className="flex items-center justify-center w-9 h-9 rounded-full bg-accent-subtle text-accent shrink-0">
          <Users size={18} className="lucide-inline" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="m-0 text-[15px] font-semibold text-text-strong leading-tight truncate">{team.name}</h2>
          <div className="text-[11.5px] text-muted leading-tight" data-testid="team-subtitle">
            {t('pages.membersPage.team_crewmate_count', { count: members.length })}
            {' \u00b7 '}
            {t('pages.membersPage.team_need_you_count', { count: waitingNames.size })}
          </div>
        </div>
        <button
          type="button"
          onClick={onEdit}
          className="text-[12px] text-muted hover:text-text hover:underline bg-transparent border-none cursor-pointer px-1 shrink-0"
          data-testid="team-edit"
        >
          {t('pages.membersPage.team_edit')}
        </button>
      </header>
      <div className="flex-1 min-h-0 overflow-y-auto px-4 pb-4">
        <div className="space-y-4">
          {rosterFailed ? null : members.length === 0 ? (
            <p className="m-0 py-6 text-center text-[13px] text-muted" data-testid="team-empty">
              {t('pages.membersPage.team_empty')}
            </p>
          ) : (
            <ul
              className="m-0 p-0 list-none rounded-xl border border-border bg-card divide-y divide-border"
              data-testid="team-status"
            >
              {members.map((m, i) => (
                <TeamStatusRow
                  key={m.row.name}
                  member={m}
                  waiting={waitingNames.has(m.row.name)}
                  questions={inbox.filter((item) => item.member.row.name === m.row.name && item.question !== null).length}
                  entries={activityQueries[i]?.data?.entries}
                  midnight={midnight}
                  weekStart={weekStart}
                  onOpen={() => onOpenMember(m.row)}
                />
              ))}
            </ul>
          )}
          <div className="flex flex-col md:flex-row items-stretch md:items-start gap-4">
            <section className="min-w-0 md:basis-[60%] md:shrink-0" data-testid="team-inbox">
              <SectionTitle count={inbox.length > 0 ? t('pages.membersPage.team_waiting_count', { count: inbox.length }) : undefined}>
                {t('pages.membersPage.team_needs_you')}
              </SectionTitle>
              {inboxFailed && (
                <ErrorNotice
                  message={t('pages.membersPage.team_inbox_failed')}
                  variant="inline"
                  askAgent
                  testId="team-inbox-error"
                />
              )}
              {inboxLoading && inbox.length === 0 && !inboxFailed && <ContentSkeleton rows={2} />}
              {!inboxLoading && !inboxFailed && inbox.length === 0 && (
                <p className="m-0 py-4 text-[13px] text-muted" data-testid="team-inbox-empty">
                  {t('pages.membersPage.team_nothing_waiting')}
                </p>
              )}
              <ul className="m-0 p-0 list-none space-y-2">
                {!confirmationsRetired && (
                  <AnimatePresence initial={false}>
                    {inbox.map((item) => (
                    <motion.li
                      key={`${item.member.row.name}:${item.question?.ts ?? 'waiting'}`}
                      layout={!reduceMotion}
                      initial={reduceMotion ? false : { opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0 }}
                      transition={reduceMotion ? { duration: 0 } : { duration: 0.15, ease: [0.2, 0, 0, 1] }}
                      className="rounded-xl border border-border bg-bg-elevated p-3"
                      data-testid="team-inbox-card"
                    >
                      <div className="flex items-start gap-2.5">
                        <CrewAvatar seed={item.member.row.name} avatar={item.member.row.avatar} size={28} className="mt-0.5" />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-baseline gap-2">
                            <span className="text-[13px] font-semibold text-text truncate">{item.member.row.name}</span>
                            {item.question?.ts && (
                              <span className="text-[11px] text-muted tabular-nums shrink-0">
                                {timeAgo(Date.parse(item.question.ts) / 1000)}
                              </span>
                            )}
                            <button
                              type="button"
                              onClick={() => onOpenMember(item.member.row)}
                              className="ml-auto text-[12px] text-accent hover:underline bg-transparent border-none cursor-pointer p-0 whitespace-nowrap"
                              data-testid="team-inbox-open"
                            >
                              {t('pages.membersPage.team_open_chat')}
                            </button>
                          </div>
                          {item.question ? (
                            <div
                              className="mt-1.5 rounded-2xl rounded-bl-md border border-border bg-card text-card-fg px-3 py-1.5 text-[13px] leading-[1.45] whitespace-pre-wrap break-words line-clamp-6"
                              data-testid="team-inbox-bubble"
                            >
                              {item.question.text}
                            </div>
                          ) : (
                            <p className="m-0 mt-1.5 text-[12.5px] text-warn" data-testid="team-inbox-waiting">
                              {t('pages.membersPage.team_waiting_in_chat')}
                            </p>
                          )}
                          {/* The crewmate's own answer chips. A chip DRAFTS the
                              answer in that crewmate's chat -- nothing is sent --
                              and the lead line says so in plain sight, because a
                              bare option label reads as a one-tap commit and a
                              reader who fears that never clicks. A row holds at
                              most two controls: the first answer as a chip, and
                              the rest -- when there are more than two -- behind
                              one "More answers" menu. */}
                          {item.question && item.question.options.length > 0 && (
                            <div className="mt-2" data-testid="team-inbox-options">
                              <p className="m-0 mb-1 text-[11.5px] text-muted leading-snug" data-testid="team-inbox-option-lead">
                                <PenLine size={11} className="lucide-inline mr-1 align-[-1px]" aria-hidden="true" />
                                {t('pages.membersPage.team_option_lead', { name: item.member.row.name })}
                              </p>
                              <div
                                className="flex items-center gap-1.5 flex-wrap"
                                role="group"
                                aria-label={t('pages.membersPage.team_option_hint')}
                              >
                                {(item.question.options.length <= 2 ? item.question.options : item.question.options.slice(0, 1)).map((o) => (
                                  <Btn
                                    key={o}
                                    type="button"
                                    className="px-2 py-0.5 text-[12px]"
                                    onClick={() => openWithDraft(item.member.row, confirmedKey.get(item.member.row.name) ?? '', o)}
                                    data-testid="team-inbox-option"
                                  >
                                    <PenLine size={11} className="lucide-inline mr-1 text-muted" aria-hidden="true" />
                                    {t('pages.membersPage.team_option_draft', { option: o })}
                                  </Btn>
                                ))}
                                {item.question.options.length > 2 && (
                                  <DropdownMenu>
                                    <DropdownMenuTrigger asChild>
                                      <Btn type="button" className="px-2 py-0.5 text-[12px]" data-testid="team-inbox-option-more">
                                        {t('pages.membersPage.team_option_more')}
                                        <ChevronDown size={12} className="lucide-inline ml-1" aria-hidden="true" />
                                      </Btn>
                                    </DropdownMenuTrigger>
                                    <DropdownMenuContent align="start" data-testid="team-inbox-option-menu">
                                      {item.question.options.slice(1).map((o) => (
                                        <DropdownMenuItem
                                          key={o}
                                          onSelect={() => openWithDraft(item.member.row, confirmedKey.get(item.member.row.name) ?? '', o)}
                                          data-testid="team-inbox-option-item"
                                        >
                                          {t('pages.membersPage.team_option_draft', { option: o })}
                                        </DropdownMenuItem>
                                      ))}
                                    </DropdownMenuContent>
                                  </DropdownMenu>
                                )}
                              </div>
                            </div>
                          )}
                        </div>
                      </div>
                    </motion.li>
                    ))}
                  </AnimatePresence>
                )}
              </ul>
            </section>
            <section className="min-w-0 flex-1" data-testid="team-week">
              <SectionTitle>{t('pages.membersPage.team_this_week')}</SectionTitle>
              {weekFailed && (
                <ErrorNotice
                  message={t('pages.membersPage.team_week_failed')}
                  variant="inline"
                  askAgent
                  testId="team-week-error"
                />
              )}
              {weekLoading && week.length === 0 && !weekFailed && <ContentSkeleton rows={3} />}
              {!weekLoading && !weekFailed && week.length === 0 && (
                <p className="m-0 py-4 text-[13px] text-muted" data-testid="team-week-empty">
                  {t('pages.membersPage.team_week_empty')}
                </p>
              )}
              {week.length > 0 && (
                <ul className="m-0 p-0 list-none rounded-xl border border-border bg-card divide-y divide-border">
                  {week.map((w, i) => (
                    <li key={`${w.row.name}:${w.entry.ts}:${i}`} data-testid="team-week-row">
                      {/* A row is a moment in one crewmate's chat, so it opens
                          that chat -- the same gesture the status strip's rows
                          have, which a reader expects to carry over. */}
                      <button
                        type="button"
                        onClick={() => onOpenMember(w.row)}
                        className="w-full flex items-start gap-2.5 px-3 py-2 text-left hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer"
                        data-testid="team-week-open"
                      >
                        <CrewAvatar seed={w.row.name} avatar={w.row.avatar} size={20} className="mt-0.5" />
                        <span className="min-w-0 flex-1">
                          <span className="flex items-baseline gap-2">
                            <span className="text-[12px] font-medium text-text truncate">{w.row.name}</span>
                            <span className="text-[11px] text-muted tabular-nums whitespace-nowrap">{timeAgo(w.entry.ts)}</span>
                          </span>
                          <span className="block text-[12.5px] text-text leading-snug break-words" title={w.entry.project || undefined}>
                            {weekLineText(t, w.entry)}
                          </span>
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        </div>
      </div>
    </div>
  )
}

function weekLineText(t: TFn, entry: MemberActivityEntry): string {
  const project = entry.project ? projectLabel(entry.project) : ''
  if (entry.via === 'select_crew') {
    return project
      ? t('pages.membersPage.team_week_routed_in', { project })
      : t('pages.membersPage.team_week_routed')
  }
  return project ? t('pages.membersPage.team_week_chat_in', { project }) : t('pages.membersPage.team_week_chat')
}

function SectionTitle({ children, count }: { children: ReactNode; count?: string }) {
  return (
    <h3 className="m-0 mb-2 pt-1 flex items-baseline gap-2 text-[13px] font-semibold text-text-strong">
      {children}
      {count && <span className="text-[11px] font-normal text-muted">{count}</span>}
    </h3>
  )
}

/** One crewmate in the status strip: avatar, name, state dot, what it is doing
 *  now, and its today / week counts. */
function TeamStatusRow({
  member,
  waiting,
  questions,
  entries,
  midnight,
  weekStart,
  onOpen,
}: {
  member: TeamMemberInput
  waiting: boolean
  questions: number
  entries: readonly MemberActivityEntry[] | undefined
  midnight: number
  weekStart: number
  onOpen: () => void
}) {
  const { t } = useTranslation()
  const row = member.row
  // The projection's `today` is a rolling 24-hour count, but this surface says
  // calendar "today". Derive it from the recent entries with the same local
  // midnight boundary as the member drawer; the query stands in for gateways
  // without that projection.
  const view = useMemberProjection<ActivityView>(member.slugCollides ? null : row.slug, 'activity')
  const activityEntries = (view?.recent as MemberActivityEntry[] | undefined) ?? entries
  const today = activityEntries?.filter((e) => e.ts >= midnight).length
  const week = view?.week ?? entries?.filter((e) => e.ts >= weekStart).length
  const state: TeamMemberState = teamMemberState({
    running: member.running,
    waiting,
    stopped: !!row.last_message_stopped,
  })
  // One state, one treatment: "Stopped" here is the roster row's red chip
  // (Square glyph + text-danger), so the two never read as different states.
  const dotCls =
    state === 'running' ? 'text-ok' : state === 'waiting' ? 'text-warn' : state === 'paused' ? 'text-danger' : 'text-muted'
  const nowCls =
    state === 'running'
      ? 'text-text'
      : state === 'waiting'
        ? 'text-warn'
        : state === 'paused'
          ? 'text-danger font-medium'
          : 'text-muted'
  let nowText: string
  if (state === 'running') nowText = row.last_message || t('pages.membersPage.team_state_running')
  // `questions` counts the questions the inbox could QUOTE for this crewmate
  // (at most one: the tail's newest). A wait with none to quote -- an
  // approval, a question the tail cannot parse -- is still a wait, said
  // without a count rather than as "1 question" nobody can find.
  else if (state === 'waiting')
    nowText =
      questions > 0
        ? t('pages.membersPage.team_state_waiting', { count: questions })
        : t('pages.membersPage.team_state_needs_you')
  else if (state === 'paused') nowText = t('pages.membersPage.team_state_paused')
  else nowText = row.last_active_ts
    ? t('pages.membersPage.team_state_idle_since', { ago: timeAgo(row.last_active_ts) })
    : t('pages.membersPage.team_state_idle')
  return (
    <li data-testid="team-status-row" data-state={state}>
      <button
        type="button"
        onClick={onOpen}
        className="w-full flex items-center gap-3 px-3.5 py-2 text-left hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer"
      >
        <CrewAvatar seed={row.name} avatar={row.avatar} size={32} />
        <span className="min-w-0 flex-1 flex items-center gap-3">
          <span className="w-20 shrink-0 text-[13px] font-semibold text-text truncate">{row.name}</span>
          <span
            className={cn('inline-block w-2 h-2 rounded-full shrink-0', dotCls)}
            style={{ background: 'currentColor' }}
            aria-hidden="true"
            data-testid={`team-state-${state}`}
          />
          <span className={cn('min-w-0 flex-1 text-[12.5px] truncate inline-flex items-center gap-1', nowCls)}>
            {state === 'paused' && <Square size={9} fill="currentColor" className="lucide-inline shrink-0" aria-hidden="true" />}
            <span className="truncate">{nowText}</span>
          </span>
        </span>
        {today !== undefined && week !== undefined && (
          <span className="hidden sm:inline text-[11px] text-muted tabular-nums whitespace-nowrap" data-testid="team-status-counts">
            {t('pages.membersPage.team_today_week', { today, week })}
          </span>
        )}
      </button>
    </li>
  )
}
