/**
 * The roster list's entries. `MemberRow` is one crewmate's row: it reads that
 * crewmate's own `roster` projection (one hook call per row, so a frame
 * re-renders just that row) and draws the row itself through `CrewmateRowBody`
 * (the face, name, status line and markers the switcher's rows share) plus the
 * star. `CrewmateEmptyHero` is the
 * intentionally-empty roster's one call to action, with the Advanced door.
 * `MembersPageRosterColumn` (MembersPageRoster.tsx) renders both; above md the
 * thread column (MembersPageThread.tsx) carries the hero instead.
 *
 * CAPTURED: Settings > Developer > Feature Previews shows a screenshot of the
 * Crewmates page in its "See what it looks like" dialog. A visible change here
 * makes that picture stale — re-shoot with `scripts/capture-feature-previews.mjs`.
 */
import { Plus, Star } from 'lucide-react'
import { Btn } from '../../components/ui'
import { useTranslation } from 'react-i18next'
import { type MemberRosterRow } from '../../api/client'
import CrewAvatar from '../../components/CrewAvatar'
import { type AutomationRecord } from '../../monitoring/automation'
import { motion } from 'framer-motion'
import { cn } from '../../lib/utils'
import { ROW_BOX_CLS, ROW_IDLE_CLS, ROW_ACTIVE_CLS } from '../../components/listShell'
import CrewmateRowBody from './CrewmateRowBody'
import { type MemberSignals } from './rosterFilter'
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
 *  (`running`), the needs-you and unread readings and the on-watch record stay
 *  on live slots in the parent (`signalsOf`, `automationOf`) and are not
 *  projected here. */
export function MemberRow({
  m,
  t,
  activeName,
  openMember,
  toggleStar,
  starPending,
  slotKeyOf,
  signalsOf,
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
  /** The page's one per-row reading (running / needs-you / unread / on watch /
   *  goal), the same the status filters count and the switcher's rows draw. */
  signalsOf: (m: MemberRosterRow) => MemberSignals
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
  // when present, so slotKeyOf / signalsOf and the display read the pushed
  // value. `running` is intentionally NOT overridden — it is live presence,
  // resolved from slots in the parent.
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
          {/* The row itself (face, name, status line, right-edge markers) is
              the switcher's row too: CrewmateRowBody. */}
          <CrewmateRowBody
            m={view}
            signals={signalsOf(view)}
            automation={automationOf(view)}
            slotKey={slotKeyOf(view)}
            size={36}
            surface="page"
            testIdPrefix="member"
            faceOnStage={faceOnStage}
          />
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

