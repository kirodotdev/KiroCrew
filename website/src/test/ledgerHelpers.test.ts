import { describe, it, expect } from 'vitest'
import { parseCheckboxLine, isCheckboxLine, toggleCheckboxText } from '../pages/chat/ledgerHelpers'

describe('ledgerHelpers', () => {
  describe('parseCheckboxLine', () => {
    it('parses unchecked checkbox', () => {
      const result = parseCheckboxLine('- [ ] Buy milk')
      expect(result).toEqual({ indent: '- ', checked: false, text: 'Buy milk' })
    })

    it('parses checked checkbox', () => {
      const result = parseCheckboxLine('- [x] Done task')
      expect(result).toEqual({ indent: '- ', checked: true, text: 'Done task' })
    })

    it('parses uppercase X', () => {
      const result = parseCheckboxLine('- [X] Done task')
      expect(result).toEqual({ indent: '- ', checked: true, text: 'Done task' })
    })

    it('parses with asterisk marker', () => {
      const result = parseCheckboxLine('* [ ] Use asterisk')
      expect(result).toEqual({ indent: '* ', checked: false, text: 'Use asterisk' })
    })

    it('parses indented checkbox', () => {
      const result = parseCheckboxLine('  - [x] Nested')
      expect(result).toEqual({ indent: '  - ', checked: true, text: 'Nested' })
    })

    it('returns null for non-checkbox line', () => {
      expect(parseCheckboxLine('Just a normal line')).toBeNull()
      expect(parseCheckboxLine('- A list item without checkbox')).toBeNull()
      expect(parseCheckboxLine('# Heading')).toBeNull()
      expect(parseCheckboxLine('')).toBeNull()
    })
  })

  describe('isCheckboxLine', () => {
    it('returns true for checkbox lines', () => {
      expect(isCheckboxLine('- [ ] Task')).toBe(true)
      expect(isCheckboxLine('- [x] Done')).toBe(true)
    })

    it('returns false for non-checkbox lines', () => {
      expect(isCheckboxLine('plain text')).toBe(false)
      expect(isCheckboxLine('- no checkbox')).toBe(false)
    })
  })

  describe('toggleCheckboxText', () => {
    it('checks an unchecked line', () => {
      expect(toggleCheckboxText('- [ ] Task')).toBe('- [x] Task')
    })

    it('unchecks a checked line', () => {
      expect(toggleCheckboxText('- [x] Task')).toBe('- [ ] Task')
    })

    it('unchecks uppercase X', () => {
      expect(toggleCheckboxText('- [X] Task')).toBe('- [ ] Task')
    })

    it('preserves indentation', () => {
      expect(toggleCheckboxText('  - [ ] Nested')).toBe('  - [x] Nested')
    })
  })
})
