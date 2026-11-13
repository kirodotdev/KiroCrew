/** The Ask layer's question field carries an accessible name, not just a placeholder. */
import path from 'node:path'
import { describe, expect, it } from 'vitest'

import { unnamedTextInputs } from '../../test/inputAccessibleNames'

describe('Design Critique text inputs have an accessible name', () => {
  it('AskLayer.tsx names every text input', () => {
    expect(unnamedTextInputs(path.join(__dirname, 'AskLayer.tsx'))).toEqual([])
  })
})
