import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  PROMPT_STASH_KEY,
  PROMPT_STASH_MAX,
  PROMPT_STASH_MAX_BYTES,
  addStashEntry,
  isDraftEmpty,
  isStashChord,
  loadPromptStash,
  makeStashEntry,
  planStashChord,
  removePromptStashKeys,
  snapshotPromptStash,
  otherSlotsStashKeys,
  promptStashBytes,
  promptStashKey,
  removeStashEntry,
  stashWriteFits,
} from '../utils/promptStash'
import { shortcutEntry } from '../lib/shortcutRegistry'
import type { PasteBlock } from '../utils/pasteTokens'

const chord = (over: Partial<Parameters<typeof isStashChord>[0]> = {}) => ({
  key: 's', metaKey: false, ctrlKey: true, altKey: false, shiftKey: false, ...over,
})
const block: PasteBlock = { id: 'p1', seq: 1, lines: 3, content: 'a\nb\nc' }

beforeEach(() => {
  localStorage.clear()
})

describe('isStashChord', () => {
  it('accepts Ctrl+S and Cmd+S, case-insensitively', () => {
    expect(isStashChord(chord())).toBe(true)
    expect(isStashChord(chord({ ctrlKey: false, metaKey: true }))).toBe(true)
    expect(isStashChord(chord({ key: 'S' }))).toBe(true)
  })

  it('does not claim plain S, Shift+Cmd+S, Alt+Cmd+S, other letters or IME keystrokes', () => {
    expect(isStashChord(chord({ ctrlKey: false }))).toBe(false)
    expect(isStashChord(chord({ shiftKey: true }))).toBe(false)
    expect(isStashChord(chord({ altKey: true }))).toBe(false)
    expect(isStashChord(chord({ key: 'd' }))).toBe(false)
    expect(isStashChord(chord({ isComposing: true }))).toBe(false)
  })

  it('matches the physical S key, not the glyph, so non-Latin layouts keep the chord', () => {
    // Cyrillic layout: Ctrl+physical-S reports the glyph on that key.
    expect(isStashChord(chord({ key: 'ы', code: 'KeyS' }))).toBe(true)
    // The registry's `stash-prompt` entry is positional too; a glyph 's' on
    // another physical key is not the chord.
    expect(isStashChord(chord({ key: 's', code: 'KeyD' }))).toBe(false)
    expect(isStashChord(chord({ key: 's', code: 'KeyS' }))).toBe(true)
  })
})

describe('shortcut registry', () => {
  it('lists the stash chord, and its default on both platforms is what the composer claims', () => {
    const entry = shortcutEntry('stash-prompt')!
    expect(entry.dispatch).toBe('code')
    const mac = entry.defaults.mac!
    const other = entry.defaults.other!
    expect(isStashChord({ key: mac.key, metaKey: !!mac.mod, ctrlKey: !!mac.ctrl, altKey: !!mac.alt, shiftKey: !!mac.shift })).toBe(true)
    expect(isStashChord({ key: other.key, metaKey: false, ctrlKey: !!(other.mod || other.ctrl), altKey: !!other.alt, shiftKey: !!other.shift })).toBe(true)
  })
})

describe('planStashChord', () => {
  it('stashes a non-empty draft, refuses at the cap, restores into an empty composer', () => {
    expect(planStashChord(false, 0)).toBe('stash')
    expect(planStashChord(false, PROMPT_STASH_MAX - 1)).toBe('stash')
    expect(planStashChord(false, PROMPT_STASH_MAX)).toBe('full')
    expect(planStashChord(true, 2)).toBe('restore')
    expect(planStashChord(true, 0)).toBe('none')
  })

  it('treats whitespace-only text as empty, but a paste block as content', () => {
    expect(isDraftEmpty('  \n ', [])).toBe(true)
    expect(isDraftEmpty('', [block])).toBe(false)
    expect(isDraftEmpty('hi', [])).toBe(false)
  })
})

describe('entry creation', () => {
  it('gives each entry a distinct id, timestamp, and copied blocks', () => {
    const blocks = [block]
    const a = makeStashEntry('x', blocks, 1)
    const b = makeStashEntry('x', blocks, 1)
    expect(a.id).not.toBe(b.id)
    expect(a.t).toBe(1)
    expect(a.blocks[0]).not.toBe(block)
    expect(a.blocks[0]).toEqual(block)
    expect(Object.keys(a).sort()).toEqual(['blocks', 'id', 't', 'text'])
  })
})

describe('persistence', () => {
  it('round-trips independent entries per slot, oldest first', () => {
    const newer = makeStashEntry('newer', [], 20)
    const older = makeStashEntry('older', [block], 10)
    expect(addStashEntry('chat-a', newer)).toBe(true)
    expect(addStashEntry('chat-a', older)).toBe(true)
    expect(addStashEntry('chat-b', makeStashEntry('for b', [], 15))).toBe(true)
    expect(loadPromptStash('chat-a').map(e => e.text)).toEqual(['older', 'newer'])
    expect(loadPromptStash('chat-a')[0].blocks).toEqual([block])
    expect(loadPromptStash('chat-b').map(e => e.text)).toEqual(['for b'])
    expect(loadPromptStash('chat-c')).toEqual([])
  })

  it('keeps two same-session tab writes made from the same stale snapshot', () => {
    const staleA = loadPromptStash('chat-a')
    const staleB = loadPromptStash('chat-a')
    expect(staleA).toEqual(staleB)
    expect(addStashEntry('chat-a', makeStashEntry('from A', [], 1))).toBe(true)
    expect(addStashEntry('chat-a', makeStashEntry('from B', [], 2))).toBe(true)
    expect(loadPromptStash('chat-a').map(e => e.text)).toEqual(['from A', 'from B'])
  })

  it('keeps creation order when more than 36 entries share a timestamp', () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(1)
    try {
      const entries = Array.from({ length: 40 }, (_, i) => makeStashEntry(`draft-${i}`, []))
      for (const entry of entries) expect(addStashEntry('chat-a', entry)).toBe(true)
      expect(loadPromptStash('chat-a').map(entry => entry.text)).toEqual(entries.map(entry => entry.text))
    } finally {
      now.mockRestore()
    }
  })

  it('continues to read legacy unpadded entry ids', () => {
    const legacy = { ...makeStashEntry('legacy', [], 1), id: 'stash-1-z-legacy00' }
    expect(addStashEntry('chat-a', legacy)).toBe(true)
    expect(loadPromptStash('chat-a').map(entry => entry.id)).toEqual([legacy.id])
  })

  it('isolates slot prefixes such as chat-1 and chat-10', () => {
    addStashEntry('chat-1', makeStashEntry('one', []))
    addStashEntry('chat-10', makeStashEntry('ten', []))
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['one'])
    expect(loadPromptStash('chat-10').map(e => e.text)).toEqual(['ten'])
    expect(promptStashKey('chat-1')).toBe(`${PROMPT_STASH_KEY}:chat-1:`)
  })

  it('lists only other slots\' stash keys for the reclaim offer', () => {
    addStashEntry('chat-1', makeStashEntry('own', []))
    addStashEntry('chat-10', makeStashEntry('neighbor', []))
    addStashEntry('chat-2', makeStashEntry('other', []))
    localStorage.setItem('unrelated', 'keep')

    const keys = otherSlotsStashKeys('chat-1')

    expect(keys.every(key => !key.startsWith(promptStashKey('chat-1')))).toBe(true)
    expect(keys.map(key => key.split(':')[1]).sort()).toEqual(['chat-10', 'chat-2'])
  })

  it('clears only the permanently deleted slot, not a shared-prefix neighbor', async () => {
    addStashEntry('chat-1', makeStashEntry('one', []))
    addStashEntry('chat-1', makeStashEntry('two', []))
    addStashEntry('chat-10', makeStashEntry('ten', []))
    localStorage.setItem('unrelated', 'keep')

    await removePromptStashKeys(await snapshotPromptStash('chat-1'))

    expect(loadPromptStash('chat-1')).toEqual([])
    expect(loadPromptStash('chat-10').map(e => e.text)).toEqual(['ten'])
    expect(localStorage.getItem('unrelated')).toBe('keep')
  })

  it('allows a recreated member session to stash under the same key after deletion', async () => {
    const slot = 'member-code-reviewer'
    expect(addStashEntry(slot, makeStashEntry('old session', []))).toBe(true)

    await removePromptStashKeys(await snapshotPromptStash(slot))

    expect(loadPromptStash(slot)).toEqual([])
    expect(addStashEntry(slot, makeStashEntry('new session', []))).toBe(true)
    expect(loadPromptStash(slot).map(entry => entry.text)).toEqual(['new session'])
  })

  it('removes only the snapshotted entries, keeping one stashed after the snapshot', async () => {
    const slot = 'member-code-reviewer'
    expect(addStashEntry(slot, makeStashEntry('old session', []))).toBe(true)
    const keys = await snapshotPromptStash(slot)
    expect(addStashEntry(slot, makeStashEntry('reopened session', []))).toBe(true)

    await removePromptStashKeys(keys)

    expect(loadPromptStash(slot).map(entry => entry.text)).toEqual(['reopened session'])
  })

  it('removes only the requested entry', () => {
    const keep = makeStashEntry('keep', [], 1)
    const remove = makeStashEntry('remove', [], 2)
    addStashEntry('chat-a', keep)
    addStashEntry('chat-a', remove)
    expect(removeStashEntry('chat-a', remove.id)).toBe(true)
    expect(loadPromptStash('chat-a').map(e => e.text)).toEqual(['keep'])
  })

  it('ignores corrupt and empty entries without truncating an oversized stash', () => {
    localStorage.setItem(`${promptStashKey('chat-a')}bad-json`, '{not json')
    localStorage.setItem(`${promptStashKey('chat-a')}bad-shape`, JSON.stringify({ text: 'x' }))
    localStorage.setItem(`${promptStashKey('chat-a')}empty`, JSON.stringify({ text: '   ', blocks: [], t: 1 }))
    for (let i = 0; i < PROMPT_STASH_MAX + 3; i++) {
      addStashEntry('chat-a', makeStashEntry(`t${i}`, [], i))
    }
    const clean = loadPromptStash('chat-a')
    expect(clean).toHaveLength(PROMPT_STASH_MAX + 3)
    expect(clean[0].text).toBe('t0')
    expect(clean[clean.length - 1].text).toBe(`t${PROMPT_STASH_MAX + 2}`)
  })

  it('reports a write that did not land', () => {
    expect(addStashEntry('chat-a', makeStashEntry('x', []))).toBe(true)
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError')
    })
    try {
      expect(addStashEntry('chat-a', makeStashEntry('y', []))).toBe(false)
      expect(loadPromptStash('chat-a').map(e => e.text)).toEqual(['x'])
    } finally {
      setItem.mockRestore()
    }
  })

  it('never expires or evicts one session\'s stash to make room for another', () => {
    // 80 slots x 5 KB stays under the cross-slot byte budget; the point here is
    // that no slot's entry is dropped for another slot's write.
    const big = 'x'.repeat(5_000)
    for (let i = 0; i < 80; i++) expect(addStashEntry(`chat-${i}`, makeStashEntry(`${i} ${big}`, []))).toBe(true)
    for (let i = 0; i < 80; i++) {
      const entries = loadPromptStash(`chat-${i}`)
      expect(entries).toHaveLength(1)
      expect(localStorage.getItem(`${promptStashKey(`chat-${i}`)}${entries[0].id}`)).not.toBeNull()
    }
    expect(localStorage.getItem(PROMPT_STASH_KEY)).toBeNull()
    expect(localStorage.getItem(`${PROMPT_STASH_KEY}-ts`)).toBeNull()
  })

  it('refuses a write that would push the whole stash past the byte budget, across slots', () => {
    // Two 200 KB drafts in two other sessions leave ~112 KB of the 512 KB budget.
    const fill = 'x'.repeat(200 * 1024)
    expect(addStashEntry('chat-a', makeStashEntry(fill, []))).toBe(true)
    expect(addStashEntry('chat-b', makeStashEntry(fill, []))).toBe(true)
    const before = promptStashBytes()
    expect(before).toBeGreaterThan(400 * 1024)
    expect(before).toBeLessThan(PROMPT_STASH_MAX_BYTES)
    const snapshot = Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)]))

    const oversized = makeStashEntry('y'.repeat(150 * 1024), [])
    expect(stashWriteFits('chat-c', oversized)).toBe(false)
    expect(addStashEntry('chat-c', oversized)).toBe(false)

    // Refused, not evicted: storage is byte-for-byte what it was, and the
    // other sessions' drafts are still there.
    expect(Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)]))).toEqual(snapshot)
    expect(promptStashBytes()).toBe(before)
    expect(loadPromptStash('chat-c')).toEqual([])
    expect(loadPromptStash('chat-a')).toHaveLength(1)
    expect(loadPromptStash('chat-b')).toHaveLength(1)

    // A draft that fits in the remaining room still lands.
    const small = makeStashEntry('z'.repeat(50 * 1024), [])
    expect(stashWriteFits('chat-c', small)).toBe(true)
    expect(addStashEntry('chat-c', small)).toBe(true)
    expect(loadPromptStash('chat-c').map(e => e.text.length)).toEqual([50 * 1024])
  })

  it('does not count an entry the write is about to release against the byte budget', () => {
    // A restored draft stays stored until it leaves the composer. Re-stashing
    // it replaces that copy, so the copy must not count toward the budget.
    const fill = 'x'.repeat(250 * 1024)
    expect(addStashEntry('chat-a', makeStashEntry(fill, []))).toBe(true)
    const restored = makeStashEntry('r'.repeat(150 * 1024), [])
    expect(addStashEntry('chat-b', restored)).toBe(true)
    const again = makeStashEntry(restored.text, [])

    expect(stashWriteFits('chat-b', again)).toBe(false)
    expect(stashWriteFits('chat-b', again, { releasing: restored.id })).toBe(true)
    expect(addStashEntry('chat-b', again, { releasing: restored.id })).toBe(true)
    // An id in another slot releases nothing.
    expect(stashWriteFits('chat-c', makeStashEntry(restored.text, []), { releasing: restored.id })).toBe(false)
  })

  it('counts key and value code units of every stash entry, and nothing else', () => {
    expect(promptStashBytes()).toBe(0)
    localStorage.setItem('mc-chat-drafts', 'q'.repeat(1000))
    localStorage.setItem('unrelated', '1')
    expect(promptStashBytes()).toBe(0)
    const entry = makeStashEntry('hello', [block])
    expect(addStashEntry('chat-a', entry)).toBe(true)
    const key = `${promptStashKey('chat-a')}${entry.id}`
    expect(promptStashBytes()).toBe(key.length + (localStorage.getItem(key)?.length ?? 0))
  })

  it('survives storage enumeration failures', () => {
    const key = vi.spyOn(Storage.prototype, 'key').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    try {
      expect(loadPromptStash('chat-a')).toEqual([])
    } finally {
      key.mockRestore()
    }
  })
})
