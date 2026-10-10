/** The board-collapse override store. Each override persists under its own
 *  localStorage key, so two tabs writing different overrides touch different
 *  keys — the cross-tab loss a shared blob's read-modify-write could produce
 *  is unrepresentable. Clearing is collapsed-only: an expand override survives
 *  a programmatic expansion, so a failed-and-rolled-back server expand cannot
 *  surprise-collapse a column that was explicitly opened. */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { boardColumnFromDroppableId, loadBoardFolderCollapse, persistBoardOverride, persistClearFolderOverrides, clearFolderOverrides, boardFolderCollapseBackup, adoptBoardFolderCollapse } from '../utils/boardFolderCollapse'

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

describe('host backup of the override family', () => {
  const BACKUP_KEY = 'kc-board-folder-collapsed'

  it('projects nothing while no override exists', () => {
    expect(boardFolderCollapseBackup()).toBeNull()
    expect(localStorage.getItem(BACKUP_KEY)).toBeNull()
  })

  it('projects the per-key family as one sorted JSON object without writing it', () => {
    persistBoardOverride('col-b', 'f1', false)
    persistBoardOverride('col-a', 'f2', true)
    persistBoardOverride('col-a', 'f1', true)
    expect(boardFolderCollapseBackup()).toBe('{"col-a:f1":true,"col-a:f2":true,"col-b:f1":false}')
    // The projection is read-only: the family stays the only thing stored.
    expect(localStorage.getItem(BACKUP_KEY)).toBeNull()
    expect(localStorage.length).toBe(3)
  })

  it('projects the same string regardless of the order the overrides were written in', () => {
    persistBoardOverride('col-a', 'f1', true)
    persistBoardOverride('col-b', 'f1', false)
    const first = boardFolderCollapseBackup()
    localStorage.clear()
    persistBoardOverride('col-b', 'f1', false)
    persistBoardOverride('col-a', 'f1', true)
    expect(boardFolderCollapseBackup()).toBe(first)
  })

  it('drops the projection once the last override is cleared', () => {
    persistBoardOverride('col-a', 'f1', true)
    persistClearFolderOverrides('f1')
    expect(boardFolderCollapseBackup()).toBeNull()
  })

  it('adopts a host value as per-key entries without storing the wire key', () => {
    expect(adoptBoardFolderCollapse('{"col-a:f1":true,"col-b:f2":false}')).toBe(true)
    expect(localStorage.getItem('kc-board-folder-collapsed:col-a:f1')).toBe('1')
    expect(localStorage.getItem('kc-board-folder-collapsed:col-b:f2')).toBe('0')
    expect(localStorage.getItem(BACKUP_KEY)).toBeNull()
    expect(localStorage.length).toBe(2)
    expect(Object.fromEntries(loadBoardFolderCollapse())).toEqual({ 'col-a:f1': true, 'col-b:f2': false })
    // The adopted family projects the very string the host restored.
    expect(boardFolderCollapseBackup()).toBe('{"col-a:f1":true,"col-b:f2":false}')
  })

  it('a host value replaces the family: shared pairs take its value, pairs it lacks are removed', () => {
    persistBoardOverride('col-a', 'f1', false)
    persistBoardOverride('col-c', 'f3', true)
    expect(adoptBoardFolderCollapse('{"col-a:f1":true,"col-a:f2":true}')).toBe(true)
    expect(Object.fromEntries(loadBoardFolderCollapse())).toEqual({ 'col-a:f1': true, 'col-a:f2': true })
    expect(localStorage.getItem('kc-board-folder-collapsed:col-c:f3')).toBeNull()
    expect(localStorage.length).toBe(2)
  })

  it('skips malformed entries of a host value and adopts the rest', () => {
    expect(adoptBoardFolderCollapse('{"col-a:f1":"1","no-colon":true,"col-b:f2":true}')).toBe(true)
    expect(Object.fromEntries(loadBoardFolderCollapse())).toEqual({ 'col-b:f2': true })
    expect(localStorage.length).toBe(1)
  })

  it('refuses a host value with no usable pair and leaves the family alone', () => {
    persistBoardOverride('col-a', 'f1', true)
    for (const unusable of ['garbage', '{}', '[]', '{"no-colon":true,"col-b:f2":"1"}']) {
      expect(adoptBoardFolderCollapse(unusable)).toBe(false)
      expect(Object.fromEntries(loadBoardFolderCollapse())).toEqual({ 'col-a:f1': true })
      expect(localStorage.length).toBe(1)
    }
  })

  it('a refused per-key write leaves the family as it was and reports the adoption as dropped', () => {
    // Quota refuses the third per-key write. The entry added before it must
    // go. The entry overwritten before it must come back. The entry the host
    // value does not list must stay.
    const refused = 'kc-board-folder-collapsed:col-b:f2'
    persistBoardOverride('col-a', 'f1', false)
    persistBoardOverride('col-c', 'f3', true)
    const realSet = Storage.prototype.setItem
    const setSpy = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(function (this: Storage, k: string, v: string) {
        if (k === refused) throw new DOMException('full', 'QuotaExceededError')
        return realSet.call(this, k, v)
      })
    try {
      expect(adoptBoardFolderCollapse('{"col-a:f1":true,"col-a:f2":true,"col-b:f2":true}')).toBe(false)
    } finally {
      setSpy.mockRestore()
    }
    expect(Object.fromEntries(loadBoardFolderCollapse())).toEqual({ 'col-a:f1': false, 'col-c:f3': true })
    expect(localStorage.getItem('kc-board-folder-collapsed:col-a:f2')).toBeNull()
    expect(localStorage.getItem(refused)).toBeNull()
    expect(localStorage.length).toBe(2)
  })

  it('a rollback that cannot write a longer corrupt value back removes the entry instead', () => {
    // The first per-key write replaces a corrupt entry with one byte. Quota
    // then refuses the second write and the write-back of the longer corrupt
    // value. The read skipped that entry before the call. The family must
    // read the same with the entry gone.
    const corrupt = 'kc-board-folder-collapsed:col-a:f1'
    const refused = 'kc-board-folder-collapsed:col-b:f2'
    localStorage.setItem(corrupt, 'garbage')
    persistBoardOverride('col-c', 'f3', true)
    const realSet = Storage.prototype.setItem
    const setSpy = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(function (this: Storage, k: string, v: string) {
        if (k === refused || v.length > 1) throw new DOMException('full', 'QuotaExceededError')
        return realSet.call(this, k, v)
      })
    try {
      expect(adoptBoardFolderCollapse('{"col-a:f1":true,"col-b:f2":true}')).toBe(false)
    } finally {
      setSpy.mockRestore()
    }
    expect(Object.fromEntries(loadBoardFolderCollapse())).toEqual({ 'col-c:f3': true })
    expect(localStorage.getItem(corrupt)).toBeNull()
    expect(localStorage.getItem(refused)).toBeNull()
    expect(localStorage.length).toBe(1)
  })

  it('adopting the same host value twice changes nothing the second time', () => {
    const value = '{"col-a:f1":true,"col-b:f2":false}'
    expect(adoptBoardFolderCollapse(value)).toBe(true)
    const setSpy = vi.spyOn(Storage.prototype, 'setItem')
    try {
      expect(adoptBoardFolderCollapse(value)).toBe(true)
      expect(setSpy).not.toHaveBeenCalled()
    } finally {
      setSpy.mockRestore()
    }
    expect(boardFolderCollapseBackup()).toBe(value)
    expect(localStorage.length).toBe(2)
  })
})
