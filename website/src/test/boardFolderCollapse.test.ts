/** The board-collapse override store. Each override persists under its own
 *  localStorage key, so two tabs writing different overrides touch different
 *  keys — the cross-tab loss a shared blob's read-modify-write could produce
 *  is unrepresentable. Clearing is collapsed-only: an expand override survives
 *  a programmatic expansion, so a failed-and-rolled-back server expand cannot
 *  surprise-collapse a column that was explicitly opened. */
import { describe, it, expect, beforeEach } from 'vitest'
import { boardCollapseKey, boardColumnFromDroppableId, loadBoardFolderCollapse, persistBoardOverride, persistClearFolderOverrides, clearFolderOverrides } from '../utils/boardFolderCollapse'

beforeEach(() => localStorage.clear())

describe('boardFolderCollapse persistence', () => {
  it('round-trips overrides through localStorage', () => {
    persistBoardOverride('col-a', 'f1', true)
    persistBoardOverride('col-b', 'f1', false)
    const loaded = loadBoardFolderCollapse()
    expect(loaded.get('col-a:f1')).toBe(true)
    expect(loaded.get('col-b:f1')).toBe(false)
  })

  it('a write from one tab preserves overrides another tab persisted meanwhile', () => {
    // Tab A loads (empty), tab B persists an override, then tab A toggles a
    // DIFFERENT folder. Distinct overrides live under distinct storage keys,
    // so neither write can observe (or drop) the other.
    persistBoardOverride('col-b', 'f2', true)   // "tab B"
    persistBoardOverride('col-a', 'f1', true)   // "tab A"
    const loaded = loadBoardFolderCollapse()
    expect(loaded.get('col-b:f2')).toBe(true)
    expect(loaded.get('col-a:f1')).toBe(true)
  })

  it('each override occupies its own storage key (no shared blob to interleave on)', () => {
    persistBoardOverride('col-a', 'f1', true)
    persistBoardOverride('col-b', 'f1', false)
    expect(localStorage.getItem('kc-board-folder-collapsed:col-a:f1')).toBe('1')
    expect(localStorage.getItem('kc-board-folder-collapsed:col-b:f1')).toBe('0')
    // The legacy single-blob key never appears.
    expect(localStorage.getItem('kc-board-folder-collapsed')).toBeNull()
  })

  it('skips a corrupt per-key value instead of throwing, leaving the rest intact', () => {
    localStorage.setItem('kc-board-folder-collapsed:col-a:f1', 'garbage')
    persistBoardOverride('col-b', 'f2', true)
    const loaded = loadBoardFolderCollapse()
    expect(loaded.has('col-a:f1')).toBe(false)
    expect(loaded.get('col-b:f2')).toBe(true)
  })

  it('persistClearFolderOverrides removes only collapsed overrides for that folder', () => {
    persistBoardOverride('col-a', 'f1', true)
    persistBoardOverride('col-b', 'f1', false)
    persistBoardOverride('col-a', 'f2', true)
    persistClearFolderOverrides('f1')
    const loaded = loadBoardFolderCollapse()
    expect(loaded.has('col-a:f1')).toBe(false)
    expect(loaded.get('col-b:f1')).toBe(false)  // expand override survives
    expect(loaded.get('col-a:f2')).toBe(true)   // other folder untouched
  })

  it('persistClearFolderOverrides scoped to one column leaves other columns alone', () => {
    persistBoardOverride('col-a', 'f1', true)
    persistBoardOverride('col-b', 'f1', true)
    persistClearFolderOverrides('f1', 'col-a')
    const loaded = loadBoardFolderCollapse()
    expect(loaded.has('col-a:f1')).toBe(false)
    expect(loaded.get('col-b:f1')).toBe(true)
  })
})

describe('clearFolderOverrides', () => {
  it('clears collapsed overrides for one folder across all columns, leaving other folders alone', () => {
    const m = new Map<string, boolean>([
      ['col-a:f1', true],
      ['col-b:f1', true],
      ['col-a:f2', true],
    ])
    const next = clearFolderOverrides(m, 'f1')
    expect(next.has('col-a:f1')).toBe(false)
    expect(next.has('col-b:f1')).toBe(false)
    expect(next.get('col-a:f2')).toBe(true)
  })

  it('keeps expand overrides: a programmatic expansion must not hand the column back to the server flag', () => {
    const m = new Map<string, boolean>([
      ['col-a:f1', false],  // explicitly expanded in col-a
      ['col-b:f1', true],   // collapsed in col-b
    ])
    const next = clearFolderOverrides(m, 'f1')
    expect(next.get('col-a:f1')).toBe(false)
    expect(next.has('col-b:f1')).toBe(false)
  })

  it('scopes to a single column when columnId is given', () => {
    const m = new Map<string, boolean>([
      ['col-a:f1', true],
      ['col-b:f1', true],
    ])
    const next = clearFolderOverrides(m, 'f1', 'col-a')
    expect(next.has('col-a:f1')).toBe(false)
    expect(next.get('col-b:f1')).toBe(true)
  })

  it('returns the same map instance when nothing matches (no spurious rerender)', () => {
    const m = new Map<string, boolean>([['col-a:f2', true], ['col-a:f1', false]])
    expect(clearFolderOverrides(m, 'f1')).toBe(m)
  })

  it('does not clear a folder whose id is a suffix of another (f1 vs xf1)', () => {
    const m = new Map<string, boolean>([['col-a:xf1', true]])
    const next = clearFolderOverrides(m, 'f1')
    expect(next.get('col-a:xf1')).toBe(true)
  })
})

describe('boardColumnFromDroppableId', () => {
  it('extracts the column id from a board folder droppable', () => {
    expect(boardColumnFromDroppableId('col-abc-123-folder-drop:folder-zzzz')).toBe('abc-123')
  })

  it('returns null for list-view and non-folder droppables', () => {
    expect(boardColumnFromDroppableId('folder-drop:folder-zzzz')).toBeNull()
    expect(boardColumnFromDroppableId('root-unnest-hint')).toBeNull()
  })
})
