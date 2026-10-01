/**
 * The side panel's per-chat size storage (`pages/chat/sidePanelWidth.ts`).
 *
 * Three contracts a refactor breaks silently:
 *
 * 1. A chat's key is the base key suffixed with its slot, and an EMPTY slot is
 *    the bare base key itself. A host whose thread is not confirmed yet would
 *    otherwise mint a phantom key that no chat ever reads again.
 * 2. A chat with no stored size of its own inherits the bare key before the
 *    built-in default. That is the whole upgrade path: without it, every
 *    existing install's panel snaps to 460 on first launch after the change,
 *    and every NEW chat forgets what the user last dragged.
 * 3. A save writes the chat's key AND the bare key, which is what feeds 2.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  SIDE_PANEL_HEIGHT_KEY,
  SIDE_PANEL_WIDTH_KEY,
  loadSidePanelDim,
  saveSidePanelDim,
  sidePanelDimKey,
} from '../pages/chat/sidePanelWidth'

describe('sidePanelDimKey', () => {
  it('suffixes the base key with the slot', () => {
    expect(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-a')).toBe('mc-side-panel-width:chat-a')
    expect(sidePanelDimKey(SIDE_PANEL_HEIGHT_KEY, 'chat-a')).toBe('mc-side-panel-height:chat-a')
  })

  it('keeps two chats apart and both apart from the bare key', () => {
    expect(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-a')).not.toBe(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-b'))
    expect(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-a')).not.toBe(SIDE_PANEL_WIDTH_KEY)
  })

  it('is the bare key for an empty slot, never a dangling suffix', () => {
    expect(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, '')).toBe(SIDE_PANEL_WIDTH_KEY)
    expect(sidePanelDimKey(SIDE_PANEL_HEIGHT_KEY, '')).toBe(SIDE_PANEL_HEIGHT_KEY)
  })
})

describe('loadSidePanelDim', () => {
  const load = (slot: string) =>
    loadSidePanelDim({ base: SIDE_PANEL_WIDTH_KEY, slot, min: 320, fallback: 460 })

  beforeEach(() => { localStorage.clear() })

  it('prefers the chat\'s own key', () => {
    localStorage.setItem(SIDE_PANEL_WIDTH_KEY, '700')
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-a'), '380')
    expect(load('chat-a')).toBe(380)
  })

  it('inherits the bare key for a chat that has none (upgrade path, and every new chat)', () => {
    localStorage.setItem(SIDE_PANEL_WIDTH_KEY, '700')
    expect(load('chat-a')).toBe(700)
    expect(load('chat-b')).toBe(700)
  })

  it('never reads another chat\'s key', () => {
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-a'), '380')
    expect(load('chat-b')).toBe(460)
  })

  it('reads only the bare key for an empty slot', () => {
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-a'), '380')
    // A dangling-suffix key is exactly what the guard exists to never read or
    // write; planted here so a guard that quietly went missing shows up.
    localStorage.setItem(`${SIDE_PANEL_WIDTH_KEY}:`, '999')
    expect(load('')).toBe(460)
    localStorage.setItem(SIDE_PANEL_WIDTH_KEY, '700')
    expect(load('')).toBe(700)
  })

  it('falls back to the default with nothing stored', () => {
    expect(load('chat-a')).toBe(460)
  })

  it('ignores a value under the floor at either key', () => {
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-a'), '10')
    expect(load('chat-a')).toBe(460)
    localStorage.setItem(SIDE_PANEL_WIDTH_KEY, '600')
    // The chat's key is still unusable, so the bare one answers.
    expect(load('chat-a')).toBe(600)
  })

  it('ignores junk', () => {
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-a'), 'wide')
    expect(load('chat-a')).toBe(460)
  })
})

describe('saveSidePanelDim', () => {
  beforeEach(() => { localStorage.clear() })

  it('writes the chat\'s key AND the bare key', () => {
    localStorage.setItem(SIDE_PANEL_WIDTH_KEY, '600')
    saveSidePanelDim({ base: SIDE_PANEL_WIDTH_KEY, slot: 'chat-a', value: 512 })
    expect(localStorage.getItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-a'))).toBe('512')
    expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe('512')
  })

  it('leaves every other chat\'s key alone', () => {
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-b'), '900')
    saveSidePanelDim({ base: SIDE_PANEL_WIDTH_KEY, slot: 'chat-a', value: 512 })
    expect(localStorage.getItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-b'))).toBe('900')
  })

  it('writes the bare key alone for an empty slot', () => {
    saveSidePanelDim({ base: SIDE_PANEL_WIDTH_KEY, slot: '', value: 512 })
    expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe('512')
    expect(localStorage.length).toBe(1)
  })

  it('round-trips through loadSidePanelDim, for the saved chat and for a new one', () => {
    saveSidePanelDim({ base: SIDE_PANEL_HEIGHT_KEY, slot: 'chat-a', value: 480 })
    const load = (slot: string) => loadSidePanelDim({ base: SIDE_PANEL_HEIGHT_KEY, slot, min: 200, fallback: 360 })
    expect(load('chat-a')).toBe(480)
    expect(load('chat-new')).toBe(480)
  })
})
