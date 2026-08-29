// Shared friendly-label lookup for scanner job phases.
//
// The backend emits raw phase strings (``starting`` / ``fetch`` / ``scanner``
// / ``cross_validate`` / ``done``) that are implementation labels, not
// user-facing copy. Both ScanProgress (main pane) and ReviewList (sidebar
// in-progress card) render human-readable descriptions via this helper so
// they show identical wording at every step of a scan — no more "Scanning..."
// on one pane and "Phase: cross_validate" on the other.
//
// An unknown phase string (backend adds a new phase, frontend hasn't been
// updated yet) falls back to a neutral "Reviewing" label rather than
// showing a raw string to the user or breaking. This keeps the two panes
// in sync even during rollout mismatches. ``null`` / empty phase (the
// tiny window between "scan started" and "first poll") shows the
// "Starting the scanners" copy so the label reads sensibly at that
// moment too.

import { i18nT } from '../../../i18n/t'

const PHASE_LABEL_KEY: Record<string, string> = {
  starting: 'apps.writingReview.phases.starting',
  fetch: 'apps.writingReview.phases.fetch',
  scanner: 'apps.writingReview.phases.scanner',
  cross_validate: 'apps.writingReview.phases.cross_validate',
  done: 'apps.writingReview.phases.done',
}

const STARTING_PHASE_LABEL_KEY = 'apps.writingReview.phases.starting'
const UNKNOWN_PHASE_LABEL_KEY = 'apps.writingReview.phases.reviewing'

export function phaseLabel(phaseName: string | null | undefined): string {
  if (!phaseName) return i18nT(STARTING_PHASE_LABEL_KEY)
  const knownKey = PHASE_LABEL_KEY[phaseName]
  if (knownKey) return i18nT(knownKey)
  return i18nT(UNKNOWN_PHASE_LABEL_KEY)
}
