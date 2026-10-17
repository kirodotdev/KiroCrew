import { describe, it, expect } from 'vitest'
import { offers, pickerQueryKey, type ComposerPickerSource } from './pickerSource'

describe('pickerSource', () => {
  it('a missing read is a menu not offered; a present but empty one is offered', () => {
    const source: ComposerPickerSource = { scope: '', slashCommands: undefined }
    expect(offers(source, 'slashCommands')).toBe(true)
    expect(offers(source, 'fileSearch')).toBe(false)
  })

  it('keeps the dashboard keys and prefixes any other scope', () => {
    expect(pickerQueryKey({ scope: '' }, ['slash-commands'])).toEqual(['slash-commands'])
    expect(pickerQueryKey({ scope: 'app:x' }, ['slash-commands'])).toEqual(['app:x', 'slash-commands'])
  })
})
