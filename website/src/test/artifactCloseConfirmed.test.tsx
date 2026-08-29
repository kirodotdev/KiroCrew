/** The artifact surface settles an unknown close the same way the App shell does. */

import { describe, it, expect } from 'vitest'
import { ARTIFACT_CLOSE_FAILURE_COPY_KEY } from '../utils/sessionCloseFailure'
import en from '../i18n/locales/en.manual.json'
import source from '../pages/ArtifactDetailPage.tsx?raw'

const catalog = en as unknown as Record<string, Record<string, Record<string, string>>>

describe('the artifact page confirms rather than clearing', () => {
  it('sets the confirmed kind on the settling snapshot', () => {
    expect(source).toMatch(
      /if \(!appliedSlots\.some\(s => s\.key === closeFailure\.key\)\) \{\s*\n\s*setCloseFailure\(\{ \.\.\.closeFailure, kind: 'confirmed' \}\)/,
    )
  })

  it('does not silently clear that state any more', () => {
    expect(source).not.toMatch(
      /if \(!appliedSlots\.some\(s => s\.key === closeFailure\.key\)\) setCloseFailure\(null\)/,
    )
  })

  it('ships the confirmation body it now reaches', () => {
    const [a, b, c] = ARTIFACT_CLOSE_FAILURE_COPY_KEY.confirmed.split('.')
    expect(catalog[a][b][c]).toBeTruthy()
  })
})
