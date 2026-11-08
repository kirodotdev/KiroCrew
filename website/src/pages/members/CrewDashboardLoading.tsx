import { Skeleton } from '../../components/ui'
import { i18nT } from '../../i18n/t'

/**
 * ONE loading state for the whole Dashboard-tab wait.
 *
 * The tab reaches the frame through three serial steps -- the lazy chunk
 * downloads, the read runs, the document mints -- and each step used to draw its
 * own placeholder (a muted "Rendering…" sentence, two thin pulsing bars, a muted
 * "Drawing…" sentence). The three looked unrelated and none of them looked like
 * the panel about to appear, so a cold open read as three brief glitches rather
 * than one load. This is the single treatment all three now render.
 *
 * Built from the project's own `Skeleton` box and theme tokens, shaped like the
 * dashboard that follows -- a title line, a stat row, and a block of content --
 * so the wait previews the panel instead of describing it. No explanatory copy:
 * a reader watching a known surface fill in does not need to be told it is
 * loading, and the three sentences were the thing that made the wait feel broken.
 *
 * Motion: `Skeleton` pulses with `animate-pulse`, which the global
 * `prefers-reduced-motion: reduce` rule in `index.css` freezes to a still box --
 * the same mechanism every other skeleton on the site relies on, so a reader who
 * asked for no motion gets a static placeholder here too.
 *
 * `role="status"` with a label names the wait for a screen reader (the boxes
 * themselves are decorative and `aria-hidden`), replacing the three separate
 * announcements the old sentences made.
 */
export default function CrewDashboardLoading() {
  return (
    <div
      className="flex h-full min-h-0 flex-col gap-4 p-4"
      role="status"
      aria-label={i18nT('pages.membersPage.dashboard_loading')}
      data-testid="crew-dashboard-loading"
    >
      <div aria-hidden className="flex flex-col gap-4">
        {/* Title line. */}
        <Skeleton className="h-5 w-1/3" />
        {/* A row of stat tiles, the shape most templates lead with. */}
        <div className="grid grid-cols-3 gap-3">
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
        </div>
        {/* The main content block the chart or table fills. */}
        <Skeleton className="h-40 w-full" />
      </div>
    </div>
  )
}
