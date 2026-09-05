// The persistent left rail: the repo dropdown, the lists it filters, then nav.
//
// Previously four stacked accordions. Three of them were not disclosures at all
// — Reviews held a status readout (now a tab + badge on the middle column) and
// Learning/Settings held one sentence of description each before navigating
// away. Collapsed drawers that only ever navigate read as clutter and cost a
// click, so the rail is now two real panels plus plain nav rows.
//
// The tabbed lists (pull requests / reviews) live here, where a third column used
// to be, under the repo picker that scopes them. Folding them into the rail
// leaves the whole rest of the window to the report, which is the widest thing
// the app renders.
import { PanelLeftClose } from 'lucide-react'

import { IconButton } from '../../../components/ui'
import { useSage } from '../context'
import MiddleColumn from './MiddleColumn'
import LearningRail from './LearningRail'
import RailHeader from './RailHeader'
import RepoSwitcher from './RepoSwitcher'

import { i18nT } from '../../../i18n/t'

export default function LeftRail({ narrow = false, onCollapse }: {
  /** True while the rail owns the whole viewport (a phone). Sizes the header row
   * for touch and drops its decoration. */
  narrow?: boolean
  /** Set ONLY while narrow, where the rail takes the whole viewport and the drag
   * handle that closes it on a desktop is not rendered. Without it a user who
   * opened the rail to look at it can leave only by picking something — and
   * re-picking the row that is already selected changes no state, so there was a
   * reachable state with no way back to the report at all. */
  onCollapse?: () => void
}) {
  const { mainView } = useSage()

  return (
    <aside className="flex-1 min-w-0 flex flex-col min-h-0 py-2 gap-2">
      {/* App identity + section nav, at the top. Issue Radar keeps its own
          bottom-most, so this is a deliberate divergence: Sage's rail leads with
          the repo dropdown, and a title under it read as a footnote to the lists
          rather than as the name of the surface you are in.

          Shared with the collapsed bar (see RailHeader), so the nav sits in the
          same place whether the rail owns the viewport or has stepped aside. */}
      <RailHeader
        narrow={narrow}
        leading={narrow && onCollapse ? (
          // Sized for touch, not for the desktop rail: it is only ever rendered
          // while narrow, where it is the one way out of a full-screen panel.
          <IconButton
            aria-label={i18nT('app.collapse_sidebar')}
            onClick={onCollapse}
            className="min-h-11 min-w-11 inline-flex items-center justify-center"
          >
            <PanelLeftClose size={16} />
          </IconButton>
        ) : undefined}
      />

      {/* The filter everything below answers to, so it sits first and collapsed
          to one row — the choice is made once, the lists are scanned constantly. */}
      {/* The rail's upper panel belongs to whichever section is active: the repo
          and its lists for Reviews, namespaces for Learning, nothing for Settings.
          Showing a repo picker and a pull-request list above an unrelated screen
          was the confusing part. */}
      {mainView === 'learning' ? (
        <div className="flex-1 min-h-0 mx-2 overflow-hidden rounded-xl border border-border bg-bg-elevated shadow-sm flex flex-col">
          <LearningRail />
        </div>
      ) : mainView === 'settings' || mainView === 'local' ? (
        <div className="flex-1 min-h-0" />
      ) : (
        <>
          <RepoSwitcher />

          <div className="flex-1 min-h-0 mx-2 overflow-hidden rounded-xl border border-border bg-bg-elevated shadow-sm flex flex-col">
            <MiddleColumn />
          </div>
        </>
      )}


    </aside>
  )
}
