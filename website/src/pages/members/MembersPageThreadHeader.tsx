/**
 * The DM header that floats over the top of the thread: the folded roster's
 * switcher chip and its unread column, the back control (the roster drawer on
 * a phone), the identity pill that opens the Profile card, and the side
 * panel's opener. On a phone it moves into the shell's single top bar.
 *
 * CAPTURED: Settings > Developer > Feature Previews shows a screenshot of the
 * Crewmates page in its "See what it looks like" dialog. A visible change here
 * makes that picture stale — re-shoot with `scripts/capture-feature-previews.mjs`.
 */
import { type NavigateFunction, type Location, type SetURLSearchParams } from 'react-router-dom'
import { ArrowLeft, ChevronRight } from 'lucide-react'
import { PanelLeftLight, PanelLeftSolid, PanelRightSolid } from '../../components/icons/panels'
import { useTranslation } from 'react-i18next'
import { type MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import CrewMonitoredAvatar from '../../components/crew/CrewMonitoredAvatar'
import Glass from '../../components/Glass'
import CrewmateSwitcher, { CrewmateUnreadColumn } from './CrewmateSwitcher'
import { createPortal } from 'react-dom'
import { uiLocation } from '../../uiLocations/uiLocation'
import { type MembersPageProfileFlight, type MembersPageProfileState } from './MembersPageProfile'
import { type MembersPageRosterView } from './MembersPageRoster'
import { type MembersPageThreadState, type MembersPageThreadView } from './MembersPageThread'
import { type ProfileCardOpen } from './membersPageModel'
import { type MembersPageDrawers } from './useMembersPageDrawers'
import { type MembersPageLayout } from './useMembersPageLayout'
import { type MembersPageNavigation } from './useMembersPageNavigation'
import { type MembersPageRosterSignals } from './useMembersPageRoster'

interface MembersPageThreadHeaderProps {
  layout: MembersPageLayout
  profileCard: MembersPageProfileState
  drawers: MembersPageDrawers
  rosterView: MembersPageRosterView
  thread: MembersPageThreadState
  threadView: MembersPageThreadView
  rosterSignals: MembersPageRosterSignals
  profileFlight: MembersPageProfileFlight
  navigation: MembersPageNavigation
  navigate: NavigateFunction
  location: Location
  setSearchParams: SetURLSearchParams
  profile: ProfileCardOpen | null
  openQuick: () => void
  createHeld: string | null
  active: MemberRosterRow
}

/** The thread header, portaled into the shell's top bar while the page has one there. */
export function MembersPageThreadHeader({
  layout, profileCard, drawers, rosterView, thread, threadView, rosterSignals, profileFlight, navigation,
  navigate, location, setSearchParams, profile, openQuick, createHeld, active,
}: MembersPageThreadHeaderProps) {
  const { t } = useTranslation()
  const { rosterPinned, switcherSlotRef, setRosterPin, panelVisible, showOpener } = layout
  const { pillFaceRef, openProfile, schedGuardRef, schedAtStakeRef, requestCloseProfile, togglePanel } = profileCard
  const { rosterDrawerOpen, openRosterDrawer, rosterDrawerAvailable } = drawers
  const { isRunning, orderedMembers } = rosterView
  const { activeSlot } = thread
  const {
    threadHeaderRef, setSwitcherOpenRequest, switcherOpenRequest, topbarSlot, headerInBar, pillActivity,
  } = threadView
  const { automationOf, isLoopOn, signalsOf } = rosterSignals
  const { faceFlight } = profileFlight
  const { openMember } = navigation
  const threadHeader = (
    <header
      ref={headerInBar ? undefined : threadHeaderRef}
      // Phone: the row lives in the shell's one top bar (`#mobile-topbar-slot`,
      // the chat page's single-bar hand-off), so it is a plain flex row
      // there, with no fade and nothing to pad the transcript for.
      className={headerInBar
        ? 'grid grid-cols-[1fr_minmax(0,auto)_1fr] items-center gap-1.5 w-full min-w-0 h-full'
        : 'absolute top-0 inset-x-0 z-20 grid grid-cols-[1fr_minmax(0,auto)_1fr] items-center gap-2 px-3 py-2 pointer-events-none'}
      data-testid="member-thread-header"
      data-in-topbar={headerInBar ? '' : undefined}
    >
      {/* Fade behind the three controls: rows scrolling up into the
          band dissolve toward the page colour before they reach the
          chips, instead of printing through them; the chips sit on a
          half-faded ground and the pane's top edge stays glass. Fills
          the header's own box (`inset-0`), so it grows with the
          header and never needs a measured height of its own. */}
      {!headerInBar && <div aria-hidden className="absolute inset-0 -z-10 bg-gradient-to-b from-bg via-bg/70 to-transparent pointer-events-none" />}
      <div className="flex items-center justify-start min-w-0 gap-1 [&>*]:pointer-events-auto">
        {/* The roster, folded into one chip (crewmate-panel IA): stacked
            faces and the count; open, a searchable list that switches
            the thread. Hidden below md, where the phone's back button
            returns to the full-width roster instead. It carries the
            same per-row signals the roster's status filters read
            (`signalsOf`), so a crewmate parked on a question stays
            visible while the roster column is folded away. With the
            column pinned open beside the thread the chip only repeats
            that list, so it fades out in place (`invisible` keeps its
            box, so the identity pill does not move) until the column
            closes; reduced motion cuts instead of fading. Only the
            hide transitions `visibility`: on show it flips at once, or
            the chip would still be hidden on the frame focus moves to it. */}
        <div
          ref={switcherSlotRef}
          className={`hidden md:flex duration-200 motion-reduce:transition-none ${rosterPinned ? 'opacity-0 invisible transition-[opacity,visibility]' : 'opacity-100 visible transition-opacity'}`}
          data-testid="crewmate-switcher-slot"
        >
        <CrewmateSwitcher
          members={orderedMembers}
          activeName={active.name}
          signals={signalsOf}
          onPick={(name) => {
            const m = orderedMembers.find((row) => row.name === name)
            if (m) void openMember(m)
          }}
          onCreate={createHeld ? undefined : openQuick}
          onShowRoster={() => setRosterPin(true)}
          openRequest={switcherOpenRequest}
        />
        </div>
        <Glass
          as="button"
          variant="chip"
          radius={999}
          // Back to the roster. On a phone the roster is the drawer over
          // this chat, so the control slides it in and nothing unmounts.
          // Otherwise, when this entry was pushed from the roster on
          // this page, pop it — the browser's own Back then lands on
          // whatever preceded the roster, with no duplicate roster
          // entry. A deep link (no such state) has no roster entry
          // behind it, so drop the param in place instead.
          onClick={() => {
            if (rosterDrawerAvailable) { openRosterDrawer(); return }
            const go = () => {
              if ((location.state as { fromRoster?: boolean } | null)?.fromRoster) navigate(-1)
              else setSearchParams({}, { replace: true })
            }
            // Clearing the member param unmounts the panel subtree with the
            // Schedules form in it, so this asks like every other exit. The
            // replace branch is the one that needed it most: a replace raises no
            // `popstate`, so neither the published stake nor `NavigationBackGuard`
            // can see it, and a deep-linked crewmate on a narrow window reaches
            // it with an ordinary tap.
            if (!schedAtStakeRef.current()) { go(); return }
            void schedGuardRef.current().then((ok) => { if (ok) go() })
          }}
          className="md:hidden glass-hover inline-flex items-center justify-center w-8 h-8 rounded-full text-text cursor-pointer"
          aria-label={t('pages.membersPage.toggle_roster')}
          data-testid="member-back"
          aria-expanded={rosterDrawerAvailable ? rosterDrawerOpen : undefined}
          {...uiLocation('members.back')}
        >
          {/* On a phone this opens the roster drawer, so it wears the
              Sessions page's own drawer toggle, not a back arrow. */}
          {rosterDrawerAvailable
            ? (rosterDrawerOpen ? <PanelLeftLight size={16} /> : <PanelLeftSolid size={16} />)
            : <ArrowLeft size={16} className="lucide-inline" />}
        </Glass>
      </div>
      {/* The identity pill is one centred Glass chip holding the face,
          name and activity line. It opens Profile, not the editor; the
          card's pencil opens the shared CrewEditorDialog. While Profile
          owns an in-flow column the pill unmounts and the face travels to
          the card through its shared layoutId. With SidePanel open, Profile
          floats and the pill remains. The accessible name stays the
          crewmate; the tooltip describes the Profile action. */}
      {profile?.placement === 'column' ? (
        /* The card holds its own column and names the crewmate itself; the
           pill steps out and its face travels to the card's head (shared
           layoutId). An empty middle cell keeps the header's grid. */
        <span aria-hidden="true" data-testid="member-identity-pill-placeholder" />
      ) : (
      <Glass
        as="button"
        type="button"
        variant="chip"
        radius={999}
        // Profile is the read surface; its pencil owns the editor door.
        // A toggle, as `aria-expanded` says: with the card already open the
        // press closes it (through the draft guard — see requestCloseProfile)
        // instead of re-opening on the default tab, which remounted the card.
        onClick={() => { if (profile) requestCloseProfile(); else openProfile() }}
        className={`glass-shadow flex items-center gap-2.5 pl-3.5 pr-3.5 ${headerInBar ? 'py-0' : 'py-1.5'} min-w-0 max-w-full justify-self-center cursor-pointer text-left focus-ring pointer-events-auto`}
        title={t('pages.membersPage.profile_card')}
        aria-expanded={!!profile}
        data-testid="member-identity-pill"
        {...uiLocation('members.edit')}
      >
        {/* The same reactive CrewStateAvatar as before — a plain face,
            no scrim, no badge (issue #9425). */}
        {/* When the card docks and the pill steps out, the face flies to
            the card's head as `CrewFaceFlight`'s copy, above everything;
            this one holds its place unpainted while a flight is up. */}
        <span
          ref={pillFaceRef}
          className="relative flex shrink-0 rounded-full"
          style={faceFlight || (profile && profile.placement === 'floating') ? { visibility: 'hidden' } : undefined}
          data-testid="member-pill-face"
        >
          <CrewMonitoredAvatar
            seed={active.name}
            avatar={active.avatar}
            slotKey={activeSlot || active.slot_key}
            running={!!isRunning(active)}
            automation={automationOf(active)}
            indicatorTestId="member-pill-loop-indicator"
            // Smaller in the shell bar, which is 42px tall: the pill sits
            // there with no vertical padding, at the height of the 32px
            // controls beside it, so it does not reach the bar's edges.
            size={headerInBar ? 26 : 30}
            working="full"
          />
        </span>
        {/* The name and activity line stay open at rest: this pill is
            how the reader knows which crewmate the page is talking to,
            and a face alone does not say that (issue #19045). */}
        <div className="min-w-0 leading-tight">
          {/* Title row = the crewmate's display name; the ID is not shown. */}
          <div className="min-w-0 flex items-center gap-1.5" data-testid="member-title-row">
            <div
              className="text-[13.5px] font-semibold truncate max-w-[24rem]"
              data-testid="member-pill-name"
            >{crewDisplayName(active)}</div>
          </div>
          {/* Activity line — what the crewmate is doing right now, text
              only (the face above already carries presence, so no dot
              here). Always rendered, so the pill keeps one height
              whether the crewmate is busy or resting: a resting line
              says how long ago the thread last moved. A busy line is
              the shared status label, clamped in `pillActivity.ts`;
              `truncate` is the belt to that cap's braces. Out of the
              button's accessible name: the name is WHO the thread is
              with, and this line changes several times a turn — the
              screen-reader copy sits outside the button, below. */}
          <div
            className="text-[11px] text-muted truncate max-w-[24rem]"
            data-testid="member-pill-activity"
            data-activity={pillActivity.kind}
            aria-hidden="true"
          >{pillActivity.label}{isLoopOn(active) ? ` · ${t('pages.membersPage.loop_on')}` : ''}</div>
        </div>
        {/* The one visible sign that this chip OPENS something. Without
            it the pill and the switcher beside it are two controls
            with faces in them, and only one of them is a door to
            the Profile. Decorative: the tooltip and `aria-expanded`
            already say it for assistive tech. */}
        <ChevronRight size={14} className="lucide-inline shrink-0 text-muted" aria-hidden="true" data-testid="member-identity-pill-chevron" />
      </Glass>
      )}
      {/* The panel's opener. Same icon and hit-target as the chat
          page's side-panel toggle, so the two surfaces teach one
          gesture, and the dashboard's side-panel chord fires it too.
          Docked, it appears only while the panel is hidden — the open
          panel's own strip carries the close control, which is exactly
          how the chat page splits the two halves of the gesture. As an
          overlay it stays put and reads pressed while the drawer is up.
          The pin chip was removed: every member thread is pinned by
          construction (a server invariant, not a per-thread state), so
          announcing it taught the user a term for a thing that can
          never be otherwise. The member's edit entry is not a peer of
          this toggle: it is the identity pill in the middle. */}
      <div className="flex items-center justify-end min-w-0 [&>*]:pointer-events-auto">
        {/* The activity line for assistive tech: the same text, outside
            the button so it never joins the crewmate's name, and NOT a
            live region — a line that changes several times a turn
            would otherwise be announced on every change. It is in the
            reading order for a reader who asks. */}
        <span className="sr-only" data-testid="member-pill-activity-sr">{pillActivity.label}{isLoopOn(active) ? ` · ${t('pages.membersPage.loop_on')}` : ''}</span>
        {/* Glass, like the switcher opposite (#18325): on the floating
            header a bare icon sat on scrolled text. */}
        {showOpener && (
          <Glass
            as="button"
            variant="chip"
            radius={999}
            onClick={togglePanel}
            className="glass-hover flex items-center justify-center w-8 h-8 rounded-full shrink-0 text-text cursor-pointer"
            aria-pressed={panelVisible}
            aria-controls="member-side-panel"
            aria-label={t('pages.membersPage.panel_toggle')}
            title={t('pages.membersPage.panel_toggle')}
            data-testid="member-panel-toggle"
            {...uiLocation('members.details')}
          >
            <PanelRightSolid size={15} />
          </Glass>
        )}
      </div>
      {/* The unread column (issue #19045), hanging under the switcher
          chip. Absolutely placed at the header's bottom edge (pulled
          back by the header's own py-2, so the first pill sits a gap
          under the chip), so it adds NOTHING to the header's measured
          height: the thread's top inset stays the chip row's, and the
          conversation keeps scrolling up under the band -- and under
          these Glass pills -- the way it scrolls under the pill. Pills
          float over the text; they never push it. Same slot rule as the
          chip: desktop only, and gone (with the chip) while the roster
          column is pinned, which carries the same dots. */}
      <div
        className={`absolute left-3 top-full -mt-2 hidden md:flex [&>*]:pointer-events-auto duration-200 motion-reduce:transition-none ${rosterPinned ? 'opacity-0 invisible transition-[opacity,visibility]' : 'opacity-100 visible transition-opacity'}`}
        data-testid="crewmate-unread-column-slot"
      >
        <CrewmateUnreadColumn
          members={orderedMembers}
          activeName={active.name}
          signals={signalsOf}
          onPick={(name) => {
            const m = orderedMembers.find((row) => row.name === name)
            if (m) void openMember(m)
          }}
          onMore={() => setSwitcherOpenRequest((n) => n + 1)}
        />
      </div>
    </header>
  );
  return headerInBar && topbarSlot ? createPortal(threadHeader, topbarSlot) : threadHeader
}
