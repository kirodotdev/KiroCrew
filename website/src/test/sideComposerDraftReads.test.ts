import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DRAFT_TTL_MS } from '../utils/draftConstants'
import {
  SIDE_DRAFT_KEY_PREFIX,
  __resetForTests,
  clearSideDraft,
  loadSideDrafts,
  readSideDraftForSlot,
  writeSideDraft,
} from '../utils/sideComposerDrafts'

function seed(composerId: string, entry: unknown): void {
  const raw = typeof entry === 'string' ? entry : JSON.stringify(entry)
  localStorage.setItem(`${SIDE_DRAFT_KEY_PREFIX}${composerId}`, raw)
}

function storedKeys(): string[] {
  const keys: string[] = []
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i)
    if (key && key.startsWith(SIDE_DRAFT_KEY_PREFIX)) keys.push(key)
  }
  return keys
}

describe('reading a slot draft back after the composer that typed it is gone', () => {
  beforeEach(() => __resetForTests())
  afterEach(() => vi.restoreAllMocks())

  it('finds the text by slot, because a closed composer cannot ask for its own id', () => {
    writeSideDraft('composer-a', 'slot-1', 'unsent prose')
    expect(readSideDraftForSlot('slot-1')).toBe('unsent prose')
  })

  it('prefers the newer of two panes even when the older is scanned last', () => {
    seed('pane-new', { s: 'slot-1', t: Date.now() - 1_000, x: 'newer' })
    seed('pane-old', { s: 'slot-1', t: Date.now() - 9_000, x: 'older' })
    expect(readSideDraftForSlot('slot-1')).toBe('newer')
  })

  it('gives the same answer when the two panes are scanned in the opposite order', () => {
    seed('pane-old', { s: 'slot-1', t: Date.now() - 9_000, x: 'older' })
    seed('pane-new', { s: 'slot-1', t: Date.now() - 1_000, x: 'newer' })
    expect(readSideDraftForSlot('slot-1')).toBe('newer')
  })

  it('does not hand one slot the draft another slot is holding', () => {
    writeSideDraft('composer-a', 'slot-other', 'not yours')
    expect(readSideDraftForSlot('slot-1')).toBeNull()
  })

  it('withholds a draft past the TTL, whose window is long gone', () => {
    seed('stale-pane', { s: 'slot-1', t: Date.now() - DRAFT_TTL_MS - 1_000, x: 'expired' })
    expect(readSideDraftForSlot('slot-1')).toBeNull()
  })

  it('withholds a presence-only entry, which proves a draft existed but returns no prose', () => {
    seed('legacy-pane', { s: 'slot-1', t: Date.now() })
    expect(readSideDraftForSlot('slot-1')).toBeNull()
  })

  it('degrades to no recoverable draft when storage refuses enumeration', () => {
    writeSideDraft('composer-a', 'slot-1', 'unsent prose')
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    expect(readSideDraftForSlot('slot-1')).toBeNull()
  })
})

describe('a presence scan reclaims what a crashed window left behind', () => {
  beforeEach(() => __resetForTests())
  afterEach(() => vi.restoreAllMocks())

  it('deletes an expired entry rather than skipping it, since nothing can revive it', () => {
    seed('stale-pane', { s: 'slot-1', t: Date.now() - DRAFT_TTL_MS - 1_000, x: 'expired' })
    expect(loadSideDrafts()).toEqual({})
    expect(storedKeys()).toEqual([])
  })

  it('deletes an entry it cannot parse, which no later read could use either', () => {
    seed('corrupt-pane', 'not json{')
    expect(loadSideDrafts()).toEqual({})
    expect(storedKeys()).toEqual([])
  })

  it('keeps a live entry the same scan walks past an expired one', () => {
    seed('stale-pane', { s: 'slot-1', t: Date.now() - DRAFT_TTL_MS - 1_000, x: 'expired' })
    writeSideDraft('live-pane', 'slot-1', 'still typing')
    expect(loadSideDrafts()).toEqual({ 'slot-1': ['live-pane'] })
    expect(readSideDraftForSlot('slot-1')).toBe('still typing')
  })

  it('lists both composers holding a draft against one slot', () => {
    writeSideDraft('pane-a', 'slot-1', 'first')
    writeSideDraft('pane-b', 'slot-1', 'second')
    expect(loadSideDrafts()['slot-1']).toHaveLength(2)
  })

  it('reports a presence-only entry as present, so the close guard still fires', () => {
    seed('legacy-pane', { s: 'slot-1', t: Date.now() })
    expect(loadSideDrafts()).toEqual({ 'slot-1': ['legacy-pane'] })
  })

  it('tolerates a key removed by another window between enumeration and the read', () => {
    writeSideDraft('composer-a', 'slot-1', 'unsent prose')
    vi.spyOn(Storage.prototype, 'getItem').mockReturnValue(null)
    expect(loadSideDrafts()).toEqual({})
    expect(readSideDraftForSlot('slot-1')).toBeNull()
  })

  it('reports no drafts rather than throwing when storage refuses the scan', () => {
    writeSideDraft('composer-a', 'slot-1', 'unsent prose')
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    expect(loadSideDrafts()).toEqual({})
  })

  it('leaves a foreign storage key alone, matching only its own prefix', () => {
    localStorage.setItem('mc-chat-drafts', '{"slot-1":"someone else"}')
    writeSideDraft('composer-a', 'slot-1', 'unsent prose')
    expect(loadSideDrafts()).toEqual({ 'slot-1': ['composer-a'] })
    expect(localStorage.getItem('mc-chat-drafts')).toBe('{"slot-1":"someone else"}')
    localStorage.removeItem('mc-chat-drafts')
  })
})

describe('an entry whose shape the store cannot trust', () => {
  beforeEach(() => __resetForTests())
  afterEach(() => vi.restoreAllMocks())

  it.each([
    ['a JSON null', 'null'],
    ['a bare number', '42'],
    ['a slot that is not a string', JSON.stringify({ s: 7, t: Date.now(), x: 'typed' })],
    ['a stamp that is not a number', JSON.stringify({ s: 'slot-1', t: 'recently', x: 'typed' })],
  ])('is discarded when it carries %s', (_shape, raw) => {
    seed('suspect-pane', raw)
    expect(loadSideDrafts()).toEqual({})
    expect(readSideDraftForSlot('slot-1')).toBeNull()
  })
})

describe('dropping a draft when storage itself is denied', () => {
  beforeEach(() => __resetForTests())
  afterEach(() => vi.restoreAllMocks())

  it('leaves the composer usable when a single clear cannot reach storage', () => {
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    expect(() => clearSideDraft('composer-a')).not.toThrow()
  })

  it('leaves the harness usable when a full reset cannot reach storage', () => {
    writeSideDraft('composer-a', 'slot-1', 'unsent prose')
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    expect(() => __resetForTests()).not.toThrow()
  })
})
