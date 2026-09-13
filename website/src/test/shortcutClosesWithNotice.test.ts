import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const source = readFileSync(
  resolve(__dirname, '../hooks/useKeyboardShortcuts.ts'),
  'utf-8',
)

describe('the close-session shortcut reports a failure like the menu path', () => {
  const at = source.search(/'close-chat':\s*\(\)\s*=>/)
  const action = source.slice(at, at + 1400)

  it('routes the close through the notice-carrying helper', () => {
    expect(at, 'close-chat action missing').toBeGreaterThan(-1)
    expect(action).toMatch(/closeSlotWithNotice\(\s*dispatch\s*,/)
  })

  it('passes the title so the notice can name the session', () => {
    expect(action).toMatch(/closeSlotWithNotice\(\s*dispatch\s*,\s*activeSlot\s*,\s*slot\?\.title/)
  })

  it('no longer dispatches the silent delete anywhere in the hook', () => {
    expect(source).not.toMatch(/\bdeleteSlot\b/)
  })
})
