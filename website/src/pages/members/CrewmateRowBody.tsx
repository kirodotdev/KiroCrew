/**
 * The one crewmate row: the face with its presence dot and on-watch ring, the
 * name, the status line and the right-edge markers. Drawn by the full roster's
 * `MemberRow` (MembersPageRosterRows.tsx) and by the switcher's option
 * (CrewmateSwitcher.tsx), so the two lists cannot disagree on what a state
 * looks like: one dot colour, one status line, one on-watch cue, drawn once.
 *
 * Everything here reads ONE `MemberSignals` record (`signalsOf`, in
 * useMembersPageRoster.ts), the same reading the status filters count, so a
 * marker can never disagree with the filter that lists it. The host owns the
 * row's chrome -- the button, its active state, the roster's star and team
 * indent, the switcher's check mark -- and hands this component the row's
 * identity, its signals and the automation record for the avatar ring.
 *
 * Markers, left to right:
 *  - the face: `CrewMonitoredAvatar` (working face, on-watch ring) with the
 *    `ok` presence dot on its corner while the member works -- an idle member
 *    shows nothing rather than a grey dot, which read as disabled;
 *  - the name line: the display name, and at its right end the goal mark (a
 *    named `Goal` glyph) while the member holds a goal;
 *  - the status line under the name: a `Stopped` chip when the thread's newest
 *    event is a Stop press, then the last-message preview, which is what
 *    truncates. On watch (an active monitor loop or the heartbeat) is the ring
 *    on the face, with an sr-only word so the state is still spoken;
 *  - the right edge: the `warn` needs-you dot for a turn parked on an approval
 *    or a question, leading the `ok` unread dot, each a named image plus an
 *    `aria-hidden` word, so colour alone never carries a state.
 */
import { Goal, Square } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import CrewMonitoredAvatar from '../../components/crew/CrewMonitoredAvatar'
import { ROW_STATUS_CLS, ROW_TITLE_CLS } from '../../components/listShell'
import { cn } from '../../lib/utils'
import type { AutomationRecord } from '../../monitoring/automation'
import type { MemberSignals } from './rosterFilter'

export interface CrewmateRowBodyProps {
  /** The row's record, the projected view when the host has one. */
  m: MemberRosterRow
  /** The live facts the markers draw, from the page's one resolver. */
  signals: MemberSignals
  /** The automation record for the member's own slot (the avatar ring). */
  automation: AutomationRecord | null
  /** The member's DM slot, for the face's working reaction. */
  slotKey: string
  /** Face diameter: 36 on the roster, 32 in the switcher's denser list. */
  size: number
  /** The surface the row sits on, for the presence dot's cut-out border:
   *  the page (`bg`) or a popover (`bg-elevated`). */
  surface: 'page' | 'elevated'
  /** Prefix of every marker's `data-testid` (`member`, `crewmate-switcher-row`). */
  testIdPrefix: string
  /** The face is centre stage in the first Mate's entrance (#23207): the row
   *  keeps its box and holds the face unpainted until it lands. */
  faceOnStage?: boolean
}

export default function CrewmateRowBody({
  m, signals, automation, slotKey, size, surface, testIdPrefix, faceOnStage = false,
}: CrewmateRowBodyProps) {
  const { t } = useTranslation()
  const needsYouLabel = t('pages.membersPage.filter_status_needs_you')
  const goalLabel = t('pages.membersPage.row_goal')
  const watchLabel = t('pages.membersPage.loop_on')
  return (
    <>
      {/* `flex`, not the blockified default: a block holding the inline-flex
          avatar opens a text line box whose strut descent grows this box
          below the face, so `items-center` on the row centred the box and
          left the face sitting a few px above the name + status block. */}
      <span
        className="relative flex shrink-0"
        style={faceOnStage ? { visibility: 'hidden' } : undefined}
        data-testid={`${testIdPrefix}-face`}
        data-entrance-empty={faceOnStage ? '' : undefined}
      >
        {/* The face reacts: it animates while the member works and flashes its
            finished / failed expression on the turn's trailing edge. The ring
            is the on-watch mark; the dot below stays presence-only. */}
        <CrewMonitoredAvatar
          seed={m.name}
          avatar={m.avatar}
          slotKey={slotKey}
          running={signals.running}
          automation={automation}
          watch={signals.patrolling}
          indicatorTestId={`${testIdPrefix}-loop-indicator`}
          size={size}
          working="subtle"
        />
        {signals.running && (
          <span
            className={cn(
              'absolute -right-0.5 -bottom-0.5 w-2.5 h-2.5 rounded-full border-2 bg-ok',
              surface === 'elevated' ? 'border-bg-elevated' : 'border-bg',
            )}
            aria-hidden="true"
            data-testid={`${testIdPrefix}-presence-dot`}
          />
        )}
      </span>
      <span className="min-w-0 flex-1">
        {/* The row's top-right corner is its free space, so the goal mark
            sits there (`ml-auto`, at the name line's far end): a named glyph,
            no word, because a goal is the ordinary state of a working crewmate
            and a badge for it on every row would be noise. The name truncates;
            the mark does not. */}
        <span className="flex items-center gap-1.5 min-w-0">
          <span className={`block ${ROW_TITLE_CLS} font-semibold text-text truncate min-w-0`}>{crewDisplayName(m)}</span>
          {signals.goal && (
            <span
              className="ml-auto shrink-0 inline-flex text-muted"
              role="img"
              aria-label={goalLabel}
              title={goalLabel}
              data-testid={`${testIdPrefix}-goal-mark`}
            >
              <Goal size={13} aria-hidden="true" />
            </span>
          )}
        </span>
        {/* Presence rides the avatar dot and on watch rides the avatar ring, so
            no visible Idle / Working / On watch word here; both states still
            reach a screen reader through the sr-only words.
            The chip is `shrink-0` so the preview, not a label, truncates.
            `Stopped` is localized HERE, not sent as a word from the server,
            whose preview is computed without the client's locale; the server
            flag is false once a newer real message lands, so the chip cannot
            outlive the stop. */}
        <span className={`flex items-center gap-1 ${ROW_STATUS_CLS} text-muted min-w-0`}>
          {/* The presence dot and the ring are both aria-hidden, so each state
              reaches a screen reader as a word here. */}
          {signals.running && (
            <span className="sr-only" data-testid={`${testIdPrefix}-working-label`}>{t('pages.membersPage.drawer_working')}</span>
          )}
          {signals.patrolling && (
            <span className="sr-only" data-testid={`${testIdPrefix}-loop-label`}>{watchLabel}</span>
          )}
          {m.last_message_stopped && (
            <span
              className="inline-flex items-center gap-0.5 shrink-0 font-medium text-danger"
              data-testid={`${testIdPrefix}-stopped-indicator`}
            >
              <Square size={9} fill="currentColor" className="lucide-inline" aria-hidden="true" />
              {t('pages.membersPage.stopped_indicator')}
            </span>
          )}
          <span className="block truncate min-w-0">{m.last_message || '\u00a0'}</span>
        </span>
      </span>
      {/* Needs-you leads the unread dot: it is the one that asks something of
          the reader. The word is `aria-hidden` because the named dot already
          puts the fuller sentence in the row's name. */}
      {signals.needsYou && (
        <span className="flex items-center gap-1 shrink-0" data-testid={`${testIdPrefix}-needs-you`}>
          <span
            className="w-2 h-2 rounded-full shrink-0 bg-warn"
            role="img"
            aria-label={needsYouLabel}
            title={needsYouLabel}
            data-testid={`${testIdPrefix}-needs-you-dot`}
          />
          <span className="text-[11px] text-muted whitespace-nowrap" aria-hidden="true">{t('pages.membersPage.team_needs_you')}</span>
        </span>
      )}
      {/* Unread on the right edge, the IM convention and where the rail badge
          sits. The STATUS token (`--ok`), like ChatSidebar's unread dot
          (#10479/#10488), not brand accent, which a theme also spends on links
          and chips. A real accessible name: nothing else on the row says
          "unread". */}
      {signals.unread && (
        <span
          className="w-2 h-2 rounded-full shrink-0"
          style={{ background: 'var(--ok)' }}
          role="img"
          aria-label={t('pages.membersPage.unread_message')}
          title={t('pages.membersPage.unread_message')}
          data-testid={`${testIdPrefix}-unread-dot`}
        />
      )}
    </>
  )
}
