// The rail's identity row: the app mark, its name, and the section nav
// (Reviews / Learning / Settings).
//
// It is its own component because the row is the app's ONLY navigation, and the
// rail it used to live inside disappears on a phone. While narrow the rail
// collapses to a bar across the top, and a bar that carried nothing but an
// expand glyph left the phone with no way to reach Learning or Settings from a
// report — the sections were reachable only by first reopening the rail, which
// is not a place the nav appears to be. Rendering the same row in both shapes
// keeps navigation in one fixed spot regardless of which pane owns the viewport.
//
// `leading` is the slot for the control that moves BETWEEN the two shapes: the
// back-to-list button while the bar is up, the collapse button while the rail
// owns the screen. It is a slot rather than a prop pair because only the shell
// knows which state it is in.
import { Brain, GitBranch, ListChecks, ScanSearch, Settings } from 'lucide-react'
import type { ReactNode } from 'react'

import { useSage } from '../context'
import type { MainView } from '../lib/types'

import { i18nT } from '../../../i18n/t'

const APP_VERSION = '2.0'

/** The three section destinations, in bar order.
 *
 * One table rather than three near-identical call sites, because the mapping is
 * read from TWO places: this nav, and the shell's back-to-list control (which
 * names the section it returns to and is not offered for a section whose rail
 * holds no list). Spelling it twice let the two drift the moment a section is
 * added or renamed.
 *
 * `labelKey` holds the FULL literal catalog key, not a fragment assembled at the
 * call site: the key-resolution gate can only verify a literal, and resolving it
 * here at module scope would freeze the first locale for the process — so the
 * label is resolved per render by {@link sectionLabel}. */
const SECTIONS: readonly {
  view: MainView
  labelKey: string
  icon: typeof Brain
  /** Whether this section's rail carries a list to return TO. Settings' rail is
   *  empty, so a back control there would point at nothing. */
  hasList: boolean
}[] = [
  {
    view: 'reviews',
    labelKey: 'apps.codeReviewSage.components.leftRail.reviews',
    // NOT the app's own ScanSearch mark, which sits inches away on the same bar:
    // two identical glyphs side by side gave a first-time user no way to tell the
    // decoration from the control, so the mark reads as a button and taps dead.
    // `ListChecks` is what the reviews LIST tab already uses in MiddleColumn, so
    // the section and the list it opens now look like the same thing.
    icon: ListChecks,
    hasList: true,
  },
  {
    view: 'local',
    labelKey: 'apps.codeReviewSage.components.localReview.local',
    icon: GitBranch,
    hasList: false,
  },
  {
    view: 'learning',
    labelKey: 'apps.codeReviewSage.components.leftRail.learning',
    icon: Brain,
    hasList: true,
  },
  {
    view: 'settings',
    labelKey: 'apps.codeReviewSage.components.leftRail.settings',
    icon: Settings,
    hasList: false,
  },
]

/** This section's name, resolved at call time so a language switch reaches it. */
export function sectionLabel(view: MainView): string {
  const entry = SECTIONS.find((s) => s.view === view)
  return entry ? i18nT(entry.labelKey) : ''
}

/** Whether this section's rail holds a list, i.e. whether "back to the list" is
 *  a real destination in it. */
export function sectionHasList(view: MainView): boolean {
  return SECTIONS.find((s) => s.view === view)?.hasList ?? false
}

/** One section destination, icon-only.
 *
 * Labelled for assistive tech and on hover but not in print. Two separate reasons,
 * because the desktop rail and the phone bar do not share one:
 *
 *  - On the WIDE rail, the sections and the lists below them both had a "Reviews",
 *    stacked one above the other, which read as a repeated control. A list of
 *    reviews has the stronger claim on the word, so the section keeps the glyph.
 *    That argument is about the list being visible directly below, so it says
 *    nothing about the bar, where the list is hidden.
 *  - On the BAR it is a width decision, measured rather than assumed: at a 390px
 *    viewport the row has 374px, and a labelled three-section nav beside the
 *    labelled back control measures 452-654px in 5 of 6 locales sampled (only
 *    zh-CN fits, with zero slack). Labels would have to wrap to a second line,
 *    permanently doubling the bar's height above the report. The maintainer's
 *    ruling on that trade is to keep the nav icon-only and let a tap teach what a
 *    glyph does — so the effort went into making a tap teach: no control-shaped
 *    thing on the bar is dead (see `onReselect`), and no glyph is duplicated by
 *    the decorative app mark (see the reviews entry's icon).
 *
 * `touch` is the narrow-viewport form. The desktop rail's 27px hit area clears
 * WCAG 2.2 SC 2.5.8's 24px floor but is not what a touch target is built to;
 * 44px is what Apple's HIG, Fluent and Primer all recommend, and on the bar these
 * three ARE the app's navigation rather than a corner of a wide rail. */
function NavRow({
  label, icon: Icon, active, touch, onClick,
}: {
  label: string
  icon: typeof Brain
  active: boolean
  touch?: boolean
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
      className={`inline-flex items-center justify-center rounded-md cursor-pointer transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 ${
        touch ? 'min-h-11 min-w-11' : 'p-1.5'
      } ${
        active
          ? 'bg-accent-subtle text-accent'
          : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover'
      }`}
    >
      <Icon size={15} className="flex-shrink-0" aria-hidden="true" />
    </button>
  )
}

export default function RailHeader({ leading, narrow = false, onReselect }: {
  /** Rendered first in the row: the shell's way in or out of the rail. */
  leading?: ReactNode
  /** True on a phone, in EITHER shape (the bar and the full-width rail). One flag
   * rather than a size-per-detail pair: both consequences — touch-sized nav, and
   * dropping the decoration that is competing for the width — follow from the
   * same fact, and splitting them invites the two call sites to drift. */
  narrow?: boolean
  /** Tapping the section that is ALREADY active. Without it that tap sets the
   * view it is already on, which changes no state and so does nothing visible —
   * a dead tap on the control a user is most likely to reach for. The shell
   * supplies the meaning: on the collapsed bar it opens the rail, which is the
   * "tap the active tab to pop to its root" convention and also the destination
   * the user was reaching for. Omitted where re-tapping has nothing to do. */
  onReselect?: () => void
}) {
  const { mainView, setMainView } = useSage()
  // Resolved once so the visible text and the `title` that reveals it when it
  // truncates cannot drift apart.
  const appName = i18nT('apps.codeReviewSage.components.leftRail.code_review_sage')

  return (
    <div
      className={`flex items-center gap-2 flex-shrink-0 min-w-0 ${
        narrow ? 'w-full px-1' : 'px-3 pt-1 pb-1.5'
      }`}
    >
      {leading}
      <ScanSearch size={16} className="text-accent flex-shrink-0" aria-hidden="true" />
      {/* The one flexible cell in the row, so a long section label or a wide
          locale eats into the NAME (which has a `title` and an icon beside it)
          rather than pushing the nav off the bar. */}
      <span className="min-w-0 truncate text-[14px] font-medium text-text" title={appName}>{appName}</span>
      {/* Decoration, so it is the first thing to go when the row is a phone-width
          bar. The version is ONE interpolated unit rather than a translated "v"
          glued to a number. */}
      {!narrow && (
        <span className="flex-shrink-0 text-[12px] text-muted opacity-70">
          {i18nT('apps.codeReviewSage.components.leftRail.version', { version: APP_VERSION })}
        </span>
      )}
      <nav
        className="ml-auto flex items-center gap-0.5 flex-shrink-0"
        aria-label={i18nT('apps.codeReviewSage.components.leftRail.sections')}
      >
        {/* Reviews is a peer of the other two, not an implicit default. It was
            previously reachable only through the rail's review list — which the
            other sections hide, leaving no way back to it at all. */}
        {SECTIONS.map((s) => (
          <NavRow
            key={s.view}
            label={i18nT(s.labelKey)}
            icon={s.icon}
            active={mainView === s.view}
            touch={narrow}
            // Re-tapping the active section is handed to the shell rather than
            // re-setting a view that is already set — see `onReselect`.
            onClick={() => (mainView === s.view ? onReselect?.() : setMainView(s.view))}
          />
        ))}
      </nav>
    </div>
  )
}
