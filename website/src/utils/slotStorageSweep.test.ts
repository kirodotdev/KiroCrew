/** The live `slots`-frame orphan sweep: a closed session's localStorage keys go
 *  on the next complete list, and nothing a partial, held or pre-snapshot list
 *  omits is touched. */
import { beforeEach, describe, expect, it } from 'vitest'
import { createSlotStorageSweep, type SlotStorageSweepInput } from './slotStorageSweep'

const HEIGHTS = 'vc_heights_'
const PANEL_TABS = 'mc-panel-tabs:'
const BUSY = 'mc-busy-send-mode:'

const rows = (...keys: string[]) => keys.map(key => ({ key }))
const frame = (keys: string[], over: Partial<SlotStorageSweepInput> = {}): SlotStorageSweepInput => ({
  rows: rows(...keys),
  keptKeys: keys,
  activeSlot: null,
  hold: { partial: false, heldKeys: [], holdChannels: false },
  snapshotSeenBefore: true,
  ...over,
})
const seed = (...sessions: string[]) => {
  for (const s of sessions) {
    localStorage.setItem(`${HEIGHTS}${s}`, '{}')
    localStorage.setItem(`${PANEL_TABS}${s}`, '[]')
  }
}
const has = (session: string) => localStorage.getItem(`${HEIGHTS}${session}`) !== null
  || localStorage.getItem(`${PANEL_TABS}${session}`) !== null

describe('createSlotStorageSweep', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('removes a closed session on the next complete frame and keeps the rest', () => {
    seed('chat-a', 'chat-b', 'chat-active', 'artifacts-gallery')
    localStorage.setItem(`${BUSY}no-slot`, 'queue')
    const sweep = createSlotStorageSweep()
    sweep(frame(['chat-a', 'chat-b']))
    expect(has('chat-a')).toBe(true)

    sweep(frame(['chat-b'], { activeSlot: 'chat-active' }))
    expect(has('chat-a')).toBe(false)
    expect(has('chat-b')).toBe(true)
    expect(has('chat-active')).toBe(true)
    expect(has('artifacts-gallery')).toBe(true)
    expect(localStorage.getItem(`${BUSY}no-slot`)).toBe('queue')
  })

  it('keeps live, active and held slots whose key contains a colon', () => {
    seed('slack:1.1', 'slack:2.2', 'slack:3.3', 'slack:9.9')
    localStorage.setItem(`${HEIGHTS}slack:1.1:tables1:pane:w4`, '{}')
    const sweep = createSlotStorageSweep()
    sweep(frame(['slack:1.1', 'slack:2.2', 'slack:3.3', 'slack:9.9']))
    sweep(frame(['slack:1.1'], {
      activeSlot: 'slack:2.2',
      hold: { partial: false, heldKeys: ['slack:3.3'], holdChannels: false },
    }))
    expect(has('slack:1.1')).toBe(true)
    expect(localStorage.getItem(`${HEIGHTS}slack:1.1:tables1:pane:w4`)).toBe('{}')
    expect(has('slack:2.2')).toBe(true)
    expect(has('slack:3.3')).toBe(true)
    expect(has('slack:9.9')).toBe(false)
  })

  it('keeps a live slot anchor saved under a pane, side or embed host', () => {
    const ANCHOR = 'vc_anchor3_'
    for (const host of ['pane', 'side', 'embed']) {
      localStorage.setItem(`${ANCHOR}${host}:chat-a`, '{}')
      localStorage.setItem(`${ANCHOR}${host}:chat-gone`, '{}')
    }
    const sweep = createSlotStorageSweep()
    sweep(frame(['chat-a', 'chat-gone', 'chat-b']))
    sweep(frame(['chat-a']))
    for (const host of ['pane', 'side', 'embed']) {
      expect(localStorage.getItem(`${ANCHOR}${host}:chat-a`)).toBe('{}')
      expect(localStorage.getItem(`${ANCHOR}${host}:chat-gone`)).toBeNull()
    }
  })

  it('keeps a key the dashboard list still carries after the frame', () => {
    seed('chat-a', 'chat-new')
    const sweep = createSlotStorageSweep()
    sweep(frame(['chat-a', 'chat-old']))
    sweep(frame(['chat-a'], { keptKeys: ['chat-a', 'chat-new'] }))
    expect(has('chat-new')).toBe(true)
  })

  it('walks storage only when the frame lost a key', () => {
    seed('stale')
    const sweep = createSlotStorageSweep()
    sweep(frame(['chat-a']))
    sweep(frame(['chat-a', 'chat-b']))
    expect(has('stale')).toBe(true)
  })

  it('removes nothing on a partial frame and keeps the last complete baseline', () => {
    seed('chat-a', 'chat-b')
    const sweep = createSlotStorageSweep()
    sweep(frame(['chat-a', 'chat-b']))
    sweep(frame(['chat-a'], { hold: { partial: true, heldKeys: [], holdChannels: false } }))
    expect(has('chat-b')).toBe(true)
    // chat-b closed during the restart: the next complete frame still collects it.
    sweep(frame(['chat-a']))
    expect(has('chat-b')).toBe(false)
  })

  it('spares a held key and, under holdChannels, a channel key', () => {
    seed('chat-a', 'chat-held', 'slack_1785370133.085469')
    const sweep = createSlotStorageSweep()
    sweep(frame(['chat-a', 'chat-held', 'slack_1785370133.085469']))
    sweep(frame(['chat-a'], { hold: { partial: false, heldKeys: ['chat-held'], holdChannels: true } }))
    expect(has('chat-held')).toBe(true)
    expect(has('slack_1785370133.085469')).toBe(true)
  })

  it('ignores the empty frame a reconnect sends before the first snapshot', () => {
    seed('chat-a')
    const sweep = createSlotStorageSweep()
    sweep(frame(['chat-a']))
    sweep(frame([], { snapshotSeenBefore: false }))
    expect(has('chat-a')).toBe(true)
  })
})
