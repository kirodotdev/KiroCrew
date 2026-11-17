import { onTestFinished } from 'vitest'

import { LOCKED_FEATURE_PREVIEWS, applyLockedControls } from '../utils/editionLocks'

/** Conductor mode is on in every build but LTS (`crewModeEnabled()` in
 *  `lib/crewMode.ts`). A test of the off state locks Feature Previews the way an
 *  LTS gateway does, for the current test only. Call it from a test body or a
 *  `beforeEach`. */
export function turnOffCrewModeAsLts(): void {
  applyLockedControls([LOCKED_FEATURE_PREVIEWS])
  onTestFinished(() => { applyLockedControls([]) })
}
