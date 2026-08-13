import { beforeEach, describe, expect, it } from 'vitest'
import type { GridNode } from '../hooks/useSessionGrid'
import {
  sessionSlots,
  anchorOf,
  isRealSplit,
  loadLayout,
  saveLayout,
  anchorForSlot,
  pruneToLive,
} from '../hooks/splitLayoutStore'

const sLeaf = (id: string, slot: string): GridNode => ({ type: 'leaf', id, kind: 'session', slot })
const pLeaf = (id: string): GridNode => ({ type: 'leaf', id, kind: 'placeholder' })
const split = (id: string, children: GridNode[]): GridNode => ({
  type: 'split',
  id,
  dir: 'col',
  children,
  sizes: children.map(() => 1 / children.length),
})

describe('splitLayoutStore', () => {
  beforeEach(() => localStorage.clear())

  describe('tree helpers', () => {
    it('sessionSlots / anchorOf / isRealSplit on a 2-session split', () => {
      const t = split('s', [sLeaf('a', 'chat-A'), sLeaf('c', 'chat-B')])
      expect(sessionSlots(t)).toEqual(['chat-A', 'chat-B'])
      expect(anchorOf(t)).toBe('chat-A')
      expect(isRealSplit(t)).toBe(true)
    })

    it('a [session | placeholder] seed is NOT a real split (1 session)', () => {
      const t = split('s', [sLeaf('a', 'chat-A'), pLeaf('b')])
      expect(sessionSlots(t)).toEqual(['chat-A'])
      expect(anchorOf(t)).toBe('chat-A')
      expect(isRealSplit(t)).toBe(false)
    })

    it('anchorOf is null for an empty / placeholder-only tree', () => {
      expect(anchorOf(null)).toBeNull()
      expect(anchorOf(pLeaf('b'))).toBeNull()
    })
  })

  describe('persist / restore', () => {
    it('round-trips a real split keyed by its anchor', () => {
      const t = split('s', [sLeaf('a', 'chat-A'), sLeaf('c', 'chat-B')])
      saveLayout(null, t)
      expect(loadLayout('chat-A')).toEqual(t)
      expect(loadLayout('chat-B')).toBeNull() // keyed only by anchor, not every member
    })

    it('does NOT persist a non-real split (single session) — dissolves it', () => {
      const seed = split('s', [sLeaf('a', 'chat-A'), pLeaf('b')])
      saveLayout(null, seed)
      expect(loadLayout('chat-A')).toBeNull()
    })

    it('closing down to one session dissolves the persisted layout', () => {
      const full = split('s', [sLeaf('a', 'chat-A'), sLeaf('c', 'chat-B')])
      saveLayout(null, full)
      expect(loadLayout('chat-A')).not.toBeNull()
      // close B → tree collapses to just leaf A (single session)
      saveLayout('chat-A', sLeaf('a', 'chat-A'))
      expect(loadLayout('chat-A')).toBeNull()
    })

    it('re-keys when the anchor pane is closed (anchor moves to first remaining)', () => {
      const abc = split('s', [sLeaf('a', 'chat-A'), sLeaf('c', 'chat-B'), sLeaf('d', 'chat-C')])
      saveLayout(null, abc)
      expect(loadLayout('chat-A')).not.toBeNull()
      // close A → [B|C], anchor becomes chat-B; prevAnchor chat-A must be removed
      const bc = split('s', [sLeaf('c', 'chat-B'), sLeaf('d', 'chat-C')])
      saveLayout('chat-A', bc)
      expect(loadLayout('chat-A')).toBeNull()
      expect(loadLayout('chat-B')).toEqual(bc)
    })
  })

  describe('anchorForSlot (badge membership)', () => {
    it('finds the anchor of the split a member slot belongs to', () => {
      const t = split('s', [sLeaf('a', 'chat-A'), sLeaf('c', 'chat-B')])
      saveLayout(null, t)
      expect(anchorForSlot('chat-A')).toBe('chat-A')
      expect(anchorForSlot('chat-B')).toBe('chat-A') // guest member resolves to the anchor
      expect(anchorForSlot('chat-Z')).toBeNull()
      expect(anchorForSlot(null)).toBeNull()
    })
  })

  describe('pruneToLive', () => {
    it('drops dead session panes and collapses', () => {
      const t = split('s', [sLeaf('a', 'chat-A'), sLeaf('c', 'chat-B')])
      const pruned = pruneToLive(t, new Set(['chat-A']))
      expect(pruned).toEqual(sLeaf('a', 'chat-A')) // single child split collapses to the leaf
    })

    it('keeps live panes in a 3-way split', () => {
      const abc = split('s', [sLeaf('a', 'chat-A'), sLeaf('c', 'chat-B'), sLeaf('d', 'chat-C')])
      const pruned = pruneToLive(abc, new Set(['chat-A', 'chat-C']))
      expect(sessionSlots(pruned)).toEqual(['chat-A', 'chat-C'])
    })

    it('returns null when every pane is dead', () => {
      const t = split('s', [sLeaf('a', 'chat-A'), sLeaf('c', 'chat-B')])
      expect(pruneToLive(t, new Set(['chat-Z']))).toBeNull()
    })
  })
})
