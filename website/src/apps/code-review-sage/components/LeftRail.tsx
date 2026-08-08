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
import { Brain, ScanSearch, Settings } from 'lucide-react'

import { useSage } from '../context'
import MiddleColumn from './MiddleColumn'
import LearningRail from './LearningRail'
import RepoSwitcher from './RepoSwitcher'

import { i18nT } from '../../../i18n/t'
const APP_VERSION = '2.0'

/** One section destination, icon-only.
 *
 * Labelled for assistive tech and on hover but not in print: the sections and the
 * lists below them both had a "Reviews", stacked one above the other, which read
 * as a repeated control. A list of reviews has the stronger claim on the word, so
 * the section keeps the glyph and gives up the label. */
function NavRow({
  label, icon: Icon, active, onClick,
}: {
  label: string
  icon: typeof Brain
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
      // Without the app's own ring the browser draws its default blue outline,
      // which is the one control here that did not match the others.
      aria-label={label}
      title={label}
      className={`inline-flex items-center justify-center rounded-md p-1.5 cursor-pointer transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 ${
        active
          ? 'bg-accent-subtle text-accent'
          : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover'
      }`}
    >
      <Icon size={15} className="flex-shrink-0" aria-hidden="true" />
    </button>
  )
}

export default function LeftRail() {
  const { mainView, setMainView } = useSage()
  // Resolved once so the visible text and the `title` that reveals it when it
  // truncates cannot drift apart.
  const appName = i18nT('apps.codeReviewSage.components.leftRail.code_review_sage')

  return (
    <aside className="flex-1 min-w-0 flex flex-col min-h-0 py-2 gap-2">
      {/* App identity, at the top. Issue Radar keeps its own bottom-most, so this
          is a deliberate divergence: Sage's rail leads with the repo dropdown,
          and a title under it read as a footnote to the lists rather than as the
          name of the surface you are in.

          The name truncates with a `title`, and the version is ONE interpolated
          unit rather than a translated "v" glued to a number. Issue Radar's rail
          still does the latter, but that instance is already recorded as existing
          debt in the render ledger; a new surface is measured against no base, so
          every finding on it is newly written. `installed_version` in the shared
          catalog is the precedent for the interpolated form. */}
      <div className="px-3 pt-1 pb-1.5 flex items-center gap-2 flex-shrink-0 min-w-0">
        <ScanSearch size={16} className="text-accent flex-shrink-0" aria-hidden="true" />
        <span className="min-w-0 truncate text-[14px] font-medium text-text" title={appName}>{appName}</span>
        <span className="flex-shrink-0 text-[12px] text-muted opacity-70">{i18nT('apps.codeReviewSage.components.leftRail.version', { version: APP_VERSION })}</span>
        <nav className="ml-auto flex items-center gap-0.5 flex-shrink-0" aria-label={i18nT('apps.codeReviewSage.components.leftRail.sections')}>
        {/* Reviews is a peer of the other two, not an implicit default. It was
            previously reachable only through the rail's review list — which the
            other sections hide, leaving no way back to it at all. */}
        <NavRow
          label={i18nT('apps.codeReviewSage.components.leftRail.reviews')}
          icon={ScanSearch}
          active={mainView === 'reviews'}
          onClick={() => setMainView('reviews')}
        />
        <NavRow
          label={i18nT('apps.codeReviewSage.components.leftRail.learning')}
          icon={Brain}
          active={mainView === 'learning'}
          onClick={() => setMainView('learning')}
        />
        <NavRow
          label={i18nT('apps.codeReviewSage.components.leftRail.settings')}
          icon={Settings}
          active={mainView === 'settings'}
          onClick={() => setMainView('settings')}
        />
        </nav>
      </div>

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
      ) : mainView === 'settings' ? (
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
