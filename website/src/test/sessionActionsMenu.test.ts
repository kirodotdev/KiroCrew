import { describe, it, expect } from 'vitest'
import { collapseGroups } from '../components/SessionActionsMenu'

/**
 * SessionActionsMenu renders one canonical, grouped item order across all four
 * session-menu surfaces, drawing a divider only *between* non-empty groups.
 * collapseGroups is the pure core of that rule; testing it directly avoids
 * driving Radix submenus in jsdom (which needs real PointerEvents).
 */
describe('collapseGroups (SessionActionsMenu separator logic)', () => {
  it('drops falsy items within a group', () => {
    expect(collapseGroups([['a', false, 'b', null, undefined]])).toEqual([['a', 'b']])
  })

  it('drops groups that become empty, so no stray divider is drawn for them', () => {
    // [meta empty] [session has items] [share empty] [close has item]
    const out = collapseGroups([
      [false, null],
      ['reveal', 'rename'],
      [undefined],
      ['close'],
    ])
    expect(out).toEqual([['reveal', 'rename'], ['close']])
    // -> exactly ONE divider (between the two surviving groups)
    expect(out.length - 1).toBe(1)
  })

  it('header-like config: MCP · (session) · (share) · colour · close survive', () => {
    const out = collapseGroups([
      ['mcp'],
      ['reveal', 'move'],
      ['copy', 'slack'],
      ['colors'],
      ['close'],
    ])
    expect(out).toEqual([['mcp'], ['reveal', 'move'], ['copy', 'slack'], ['colors'], ['close']])
    expect(out.length - 1).toBe(4) // 4 dividers between 5 groups
  })

  it('sidebar-like config: no MCP/Slack/colour slots -> only 3 groups, 2 dividers', () => {
    const out = collapseGroups([
      [false], // no mcpSlot
      ['rename', 'duplicate', 'read', 'pin', 'move', 'tags'],
      ['copy', false], // copy link, no slack slot
      [false], // no colour slot
      ['close'],
    ])
    expect(out).toEqual([
      ['rename', 'duplicate', 'read', 'pin', 'move', 'tags'],
      ['copy'],
      ['close'],
    ])
    expect(out.length - 1).toBe(2)
  })

  it('returns no groups (no dividers) when everything is gated off', () => {
    expect(collapseGroups([[false], [null, undefined], [false]])).toEqual([])
  })

  it('a single surviving group draws zero dividers', () => {
    const out = collapseGroups([[false], ['close'], [undefined]])
    expect(out).toEqual([['close']])
    expect(out.length - 1).toBe(0)
  })
})
