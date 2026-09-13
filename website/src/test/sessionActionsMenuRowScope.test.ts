import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * The action is declared as a pinned-ROW item. The same component also builds the session-header
 * menu, which passes `infoSlots`, so the row-only gate has to exclude that caller too.
 *
 * Read from source because the menu renders through a portal these suites do not mount.
 */
describe('the follow-sort action is scoped to a pinned row', () => {
  const menu = readFileSync(join(__dirname, '../components/SessionActionsMenu.tsx'), 'utf-8')

  it('is gated off the header menu, which is the caller that supplies infoSlots', () => {
    const entry = menu.indexOf("key=\"follow-sort\"")
    expect(entry).toBeGreaterThan(-1)

    // The guard sits immediately above the entry it controls.
    const guard = menu.lastIndexOf('isPinned && pinnedOrderIsManual', entry)
    expect(guard).toBeGreaterThan(-1)
    expect(menu.slice(guard, entry)).toContain('!infoSlots')
  })
})
