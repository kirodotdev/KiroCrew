/**
 * The roster list's entries. `MemberRow` is one crewmate's row: it reads that
 * crewmate's own `roster` projection (one hook call per row, so a frame
 * re-renders just that row) and draws the face, name, status line, unread and
 * needs-you marks, the loop indicator and the star. `CrewmateEmptyHero` is the
 * intentionally-empty roster's one call to action, with the Advanced door.
 * `MembersPageRosterColumn` (MembersPageRoster.tsx) renders both; above md the
 * thread column (MembersPageThread.tsx) carries the hero instead.
 *
 * CAPTURED: Settings > Developer > Feature Previews shows a screenshot of the
 * Crewmates page in its "See what it looks like" dialog. A visible change here
 * makes that picture stale — re-shoot with `scripts/capture-feature-previews.mjs`.
 */
import { Plus, Square, Star } from 'lucide-react'
import { Badge, Btn } from '../../components/ui'
import { useTranslation } from 'react-i18next'
import { type MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import CrewAvatar from '../../components/CrewAvatar'
import CrewMonitoredAvatar from '../../components/crew/CrewMonitoredAvatar'
import { isAutomationActive, type AutomationRecord } from '../../monitoring/automation'
import { motion } from 'framer-motion'
import { cn } from '../../lib/utils'
import { ROW_BOX_CLS, ROW_IDLE_CLS, ROW_ACTIVE_CLS, ROW_TITLE_CLS, ROW_STATUS_CLS } from '../../components/listShell'
import { useMemberProjection } from '../../state/useMemberProjection'
import type { RosterView } from '../../state/memberProjectionTypes'
import { uiLocation } from '../../uiLocations/uiLocation'
import { guidePick } from '../../uiLocations/targetRegistry'
import { type TFn } from './membersPageModel'

/** One roster row. Extracted so `useMemberProjection` is called once PER ROW
 *  (a hook cannot run inside the parent's `.map`), letting a `member_projection`
 *  frame re-render just this row. The projected roster view overrides the
 *  server row field-by-field when present; a field the projection omits (or a
 *  gateway with no projections block) falls back to the row. Presence
 *  (`running`) and the driving list stay on live slots in the parent and are
 *  not projected here. */
export function MemberRow({
  m,
  t,
  activeName,
  openMember,
  toggleStar,
  starPending,
  slotKeyOf,
  isRunning,
  isUnread,
  isNeedsYou,
  automationOf,
  reduceMotion,
  scrollActiveRowIntoView,
  slugCollides,
  indented,
  faceOnStage,
}: {
  m: MemberRosterRow
  t: TFn
  activeName: string
  openMember: (m: MemberRosterRow) => void
  toggleStar: (m: MemberRosterRow) => void
  starPending: Set<string>
  slotKeyOf: (m: MemberRosterRow) => string
  isRunning: (m: MemberRosterRow) => boolean | undefined
  isUnread: (m: MemberRosterRow) => boolean
  /** The crewmate's turn is parked on an approval or a question — the same
   *  reading the status filter and the switcher row take (`signalsOf`). */
  isNeedsYou: (m: MemberRosterRow) => boolean
  /** The normalized automation record for this crewmate's own slot. */
  automationOf: (m: MemberRosterRow) => AutomationRecord | null
  reduceMotion: boolean | null
  scrollActiveRowIntoView: (el: HTMLButtonElement | null) => void
  slugCollides: boolean
  /** The row sits under a team header: stepped in so the header reads as its group. */
  indented: boolean
  /** This crewmate's face is centre stage in the first Mate's entrance (#23207):
   *  the row keeps its box and holds the face unpainted until it lands. */
  faceOnStage: boolean
}) {
  // Withheld for a colliding slug, exactly as the page's merged list and drawer
  // do: this row shares its slug with another member, so a slug-keyed frame
  // cannot say which of the two it describes.
  const roster = useMemberProjection<RosterView>(slugCollides ? null : m.slug, 'roster')
  // The projected view over the server row: projection wins field-by-field
  // when present, so slotKeyOf / isRunning / isUnread and the display read the
  // pushed value. `running` is intentionally NOT overridden — it is live
  // presence, resolved from slots in the parent.
  //
  // `last_message` goes the other way, and the direction is the point.
  // Every other field here is config-derived, so the event log is where it is
  // written and the projection IS the record. A message preview is not: the
  // server row carries it from the conversation transcript, which is the store
  // the message was persisted through, and the member/message event is a second
  // copy appended afterwards on a best-effort hook. When that append is refused
  // the projection keeps the PREVIOUS message, so giving it precedence renders a
  // stale preview over the fresh transcript value sitting beside it in the same
  // payload -- and nothing on the card says which of the two it is showing. The
  // projection still fills in when the row has no transcript value at all, which
  // is what a pushed frame is for.
  //
  // `last_active_ts` takes the GREATER of the two instead, matching the merged
  // list (`members`, in useMembersPageRoster.ts): both are readings of the one
  // folded value (the server row reads it off the same roster projection), so
  // the newer reading is simply the right one, and max keeps a frozen baseline
  // from walking a row backwards.
  const view: MemberRosterRow = roster
    ? {
        ...m,
        kiro_agent: roster.kiro_agent ?? m.kiro_agent,
        workspace: roster.workspace ?? m.workspace,
        memory_store: roster.memory_store ?? m.memory_store,
        model: roster.model ?? m.model,
        source: roster.source ?? m.source,
        starred: roster.starred ?? m.starred,
        avatar: roster.avatar ?? m.avatar,
        slot_key: roster.slot_key ?? m.slot_key,
        last_active_ts: Math.max(m.last_active_ts ?? 0, roster.last_active_ts ?? 0) || undefined,
        last_message: m.last_message || roster.last_message,
      }
    : m
  const automation = automationOf(view)
  const loopOn = isAutomationActive(automation)
  // `layout` on the row, so the ONE move the recency re-sort makes is
  // followable: a row relocating to the top on the user's own send is a
  // persistent, already-identified element changing place, and an instant
  // teleport makes the reader re-find it. Keyed by the crew NAME (the row's
  // identity), which is what lets framer measure the same element across the
  // re-order. Off under reduced motion — the row still lands in its new place,
  // it just does not travel — and `layout="position"` so only the offset is
  // animated, never the row's size, which would fight the star and the preview
  // reflowing in the same frame.
  return (
      <motion.li
        key={view.name}
        {...guidePick(view.name)}
        layout={reduceMotion ? false : 'position'}
        transition={{ type: 'spring', stiffness: 520, damping: 42 }}
        className="group/row relative"
      >
        {/* ChatSidebar's own row recipe (components/listShell), so the
            two conversation lists read as one family; pr-8 widens the
            right padding over ROW_BOX_CLS's pr-3 to hold the star. The
            star is a SIBLING of the row button, not a child: a button
            inside a button is invalid HTML and breaks keyboard
            activation. It is absolutely placed over the row's right
            padding so the row keeps its single click target and the
            label its width. */}
        <button
          onClick={() => openMember(view)}
          // The open row keeps itself in view: a member opened by URL
          // (a deep link, the crew manager's post-create landing) can sit
          // below the fold of a long roster, and a thread with no visible
          // row looks like a member that was never added (#9513).
          ref={view.name === activeName ? scrollActiveRowIntoView : undefined}
          className={cn(
            'w-full flex items-center gap-2.5 text-sm text-left transition-all select-none',
            ROW_BOX_CLS, 'pr-8',
            // Grouped-row indent = ROW_BOX_CLS left pad (14) + 14: `pl-7` 28.
            indented && 'pl-7',
            view.name === activeName ? ROW_ACTIVE_CLS : ROW_IDLE_CLS,
          )}
          aria-current={view.name === activeName ? 'true' : undefined}
        >
          <span
            className="relative shrink-0"
            style={faceOnStage ? { visibility: 'hidden' } : undefined}
            data-testid="member-row-face"
            data-entrance-empty={faceOnStage ? '' : undefined}
          >
            {/* The face reacts: it animates while the member works and
                flashes its finished / failed expression on the turn's
                trailing edge. The dot below stays presence-only — a
                finished turn is not presence. */}
            <CrewMonitoredAvatar
              seed={view.name}
              avatar={view.avatar}
              slotKey={slotKeyOf(view)}
              running={!!isRunning(view)}
              automation={automation}
              indicatorTestId="member-loop-indicator"
              size={36}
              working="subtle"
            />
            {/* Presence dot renders only while the member is working —
                an idle member shows nothing rather than a gray dot,
                which read as a broken/disabled state. */}
            {isRunning(view) && (
              <span
                className="absolute -right-0.5 -bottom-0.5 w-2.5 h-2.5 rounded-full border-2 border-bg bg-ok"
                aria-hidden="true"
                data-testid="member-presence-dot"
              />
            )}
            {/* On watch is drawn by CrewMonitoredAvatar from the same record
                this row uses for its visible label. */}
          </span>
          <span className="min-w-0 flex-1">
            <span className={`block ${ROW_TITLE_CLS} font-semibold text-text truncate`}>{crewDisplayName(view)}</span>
            {/* Last-message preview, like a session row — presence
                already rides the avatar dot, so a textual Idle/Working
                label says nothing the dot does not. A "Stopped" chip
                leads the preview when the thread's NEWEST event is a
                Stop press: the server skips the stop card's JSON, so the
                preview is the last conversational line, which reads as
                ongoing work on a thread the user has stopped — the chip
                is the honest marker over it. It is localized HERE, not
                sent as a word from the server, whose preview is computed
                without the client's locale. The chip is `shrink-0` so
                the preview, not the label, is what truncates. The server
                flag is false once a newer real message lands, so the chip
                cannot outlive the stop. */}
            <span className={`flex items-center gap-1 ${ROW_STATUS_CLS} text-muted min-w-0`}>
              {view.last_message_stopped && (
                <span
                  className="inline-flex items-center gap-0.5 shrink-0 font-medium text-danger"
                  data-testid="member-stopped-indicator"
                >
                  <Square size={9} fill="currentColor" className="lucide-inline" aria-hidden="true" />
                  {t('pages.membersPage.stopped_indicator')}
                </span>
              )}
              {/* The mark's word, so it reads without a tooltip (touch,
                  reduced motion). Accent, like the mark. */}
              {loopOn && (
                <Badge
                  variant="muted"
                  className="shrink-0 px-1.5 py-0 text-[11px] font-sans bg-accent-subtle text-accent"
                  data-testid="member-loop-label"
                >
                  {t('pages.membersPage.loop_on')}
                </Badge>
              )}
              <span className="block truncate min-w-0">{view.last_message || '\u00a0'}</span>
            </span>
          </span>
          {/* Needs-you marker on the row's right edge, leading the unread
              dot: the switcher's own cue (CrewmateSwitcher's row), so the
              full roster and the folded one agree about who is parked on
              a question. A `warn` dot that is a named image plus the same
              muted word, `aria-hidden` because the dot's name already says
              the fuller sentence; the two labels are the existing
              `filter_status_needs_you` / `team_needs_you` keys. */}
          {isNeedsYou(view) && (
            <span className="flex items-center gap-1 shrink-0" data-testid="member-needs-you">
              <span
                className="w-2 h-2 rounded-full shrink-0 bg-warn"
                role="img"
                aria-label={t('pages.membersPage.filter_status_needs_you')}
                title={t('pages.membersPage.filter_status_needs_you')}
                data-testid="member-needs-you-dot"
              />
              <span className="text-[11px] text-muted whitespace-nowrap" aria-hidden="true">{t('pages.membersPage.team_needs_you')}</span>
            </span>
          )}
          {/* Unread marker on the row's right edge — the IM convention
              (and where the rail badge sits), vertically centered by the
              row's items-center. w-2 h-2 filled with the STATUS token
              like ChatSidebar's unread dot (#10479/#10488) — not brand
              accent, which a theme also spends on links and chips — with
              a real accessible name: nothing else on the row says
              "unread". The left side is taken — presence rides the
              avatar. */}
          {isUnread(view) && (
            <span
              className="w-2 h-2 rounded-full shrink-0"
              style={{ background: 'var(--ok)' }}
              role="img"
              aria-label={t('pages.membersPage.unread_message')}
              title={t('pages.membersPage.unread_message')}
              data-testid="member-unread-dot"
            />
          )}
        </button>
        {/* Star: always rendered when starred. Unstarred: visible below md
            (touch has no hover or keyboard focus to reveal it), hover /
            focus-revealed at md+ so a desktop roster stays quiet. Never
            hidden from AT — opacity, not display. */}
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation()
            toggleStar(view)
          }}
          aria-pressed={!!view.starred}
          disabled={starPending.has(view.name)}
          aria-label={t(view.starred ? 'pages.membersPage.unstar' : 'pages.membersPage.star', { name: view.name })}
          title={t(view.starred ? 'pages.membersPage.unstar' : 'pages.membersPage.star', { name: view.name })}
          // 24x24 minimum target (the icon is 13px): a touch that lands beside
          // the glyph must hit the star, not the row button underneath.
          className={`absolute right-1 top-1/2 -translate-y-1/2 flex items-center justify-center w-6 h-6 rounded hover:bg-bg-hover transition-opacity ${
            view.starred
              ? 'opacity-100 text-accent'
              : 'md:opacity-0 md:group-hover/row:opacity-100 [@media(hover:none)]:opacity-100 md:focus-visible:opacity-100 text-muted'
          }`}
          data-testid={`member-star-${view.slug}`}
        >
          <Star
            size={13}
            {...(view.starred ? { fill: 'var(--accent)', stroke: 'none' } : {})}
          />
        </button>
      </motion.li>
  )
}

/** The intentionally-empty roster: one hero, one call to action. Type scale and
 *  tokens follow `EmptyState` in components/ui.tsx; the face is the real ghost at
 *  full strength rather than EmptyState's 12%-opacity icon, because here the
 *  avatar IS the subject (what a crewmate looks like), not decoration. */
export function CrewmateEmptyHero({ onCreate, onAdvanced, held, onImport }: {
  onCreate: () => void
  /** Opens the New crewmate card with its Advanced settings unfolded. */
  onAdvanced: () => void
  /**
   * Why the CTA is held, or `null` when it is live. Set while a just-created
   * crewmate's follow-up is still in flight: the roster re-read has not yet
   * replaced this hero, and a second create through it would clear the first
   * one's recovery record exactly as the header "+" would — so every door to
   * the dialog reads the same hold.
   */
  held: string | null
  /** Opens Import from file (rfc-crewmate-share.md §5.4). */
  onImport?: () => void
}) {
  const { t } = useTranslation()
  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-3 px-6 py-10 text-center animate-rise" data-testid="crewmate-empty-hero">
      <div className="mb-1 opacity-90"><CrewAvatar seed="crewmate" size={72} /></div>
      <div className="text-[17px] font-semibold text-text-strong" data-testid="crewmate-empty-title">{t('pages.membersPage.empty_title')}</div>
      <p className="m-0 max-w-[400px] text-[13.5px] leading-relaxed text-muted">{t('pages.membersPage.empty_body')}</p>
      <Btn
        primary
        onClick={onCreate}
        disabled={held !== null}
        title={held ?? undefined}
        aria-label={held ?? undefined}
        className="mt-2 h-9 px-4 text-[13.5px]"
        data-testid="crewmate-empty-cta"
        {...uiLocation('members.new')}
      >
        <Plus size={15} className="lucide-inline" aria-hidden="true" />
        {t('pages.membersPage.add_member')}
      </Btn>
      {/* Every setting stays one click away on an empty roster, where the header
          "+" menu is hidden behind this hero. */}
      <button
        type="button"
        onClick={onAdvanced}
        disabled={held !== null}
        className="text-[12.5px] text-muted underline-offset-2 hover:underline bg-transparent border-none cursor-pointer disabled:cursor-default disabled:opacity-60"
        data-testid="crewmate-empty-advanced"
        {...uiLocation('members.new-advanced')}
      >
        {t('pages.membersPage.create_advanced')}
      </button>
      {/* The header "+" menu and the switcher are both hidden here, so the
          first crewmate can only be imported from this hero. */}
      {onImport && (
        <button
          type="button"
          onClick={onImport}
          disabled={held !== null}
          className="text-[12.5px] text-muted underline-offset-2 hover:underline bg-transparent border-none cursor-pointer disabled:cursor-default disabled:opacity-60"
          data-testid="crewmate-empty-import"
          {...uiLocation('members.empty.import')}
        >
          {t('pages.membersPage.import_from_file')}
        </button>
      )}
    </div>
  )
}

