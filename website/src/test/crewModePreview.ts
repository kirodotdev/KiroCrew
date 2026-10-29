import { beforeEach } from 'vitest'

import { PREVIEW_CREW_MODE } from '../utils/previewFlags'

/** Crew Mode is a feature preview, default off (`PREVIEW_CREW_MODE`). A test of a
 *  Crew Mode surface turns it on before each test, as a person does in Settings >
 *  Developer > Feature Previews. Call it at the file's top level, after any hook of
 *  the file's own that clears `localStorage`. */
export function crewModePreviewOn(): void {
  beforeEach(() => { turnOnCrewModePreview() })
}

/** Turn the preview on now: for a test that clears `localStorage` in its own body. */
export function turnOnCrewModePreview(): void {
  localStorage.setItem(PREVIEW_CREW_MODE, '1')
}
