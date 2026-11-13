/** The project-path and dev-server URL fields carry an accessible name, not just a placeholder. */
import path from 'node:path'
import { describe, expect, it } from 'vitest'

import { unnamedTextInputs } from '../../test/inputAccessibleNames'

describe('Design Tweak text inputs have an accessible name', () => {
  it('DesignTweakPage.tsx names every text input', () => {
    expect(unnamedTextInputs(path.join(__dirname, 'DesignTweakPage.tsx'))).toEqual([])
  })
})
