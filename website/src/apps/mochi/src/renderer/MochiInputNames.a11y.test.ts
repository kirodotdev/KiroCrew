/** Mochi's colour-prompt and Petdex name fields carry an accessible name, not just a placeholder. */
import path from 'node:path'
import { describe, expect, it } from 'vitest'

import { unnamedTextInputs } from '../../../../test/inputAccessibleNames'

describe('Mochi text inputs have an accessible name', () => {
  it.each(['ColorCustomizer.tsx', 'PetdexImporter.tsx'])('%s names every text input', (file) => {
    expect(unnamedTextInputs(path.join(__dirname, file))).toEqual([])
  })
})
