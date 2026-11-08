/**
 * Keep side panel open (`dashboard.side_panel_always_open`) in the chat slice:
 * while on, every slot-entry path enters with the panel open, and none of them
 * saves that as the chat's own choice; turning it off puts every chat back on
 * its saved choice. A phone or embed hold that closed a panel reopens it when
 * the hold lifts, and a chat entered during the hold opens its panel then,
 * unless the user acted on the panel. A switch that falls back because the
 * chat it entered is gone puts back the reopen it found.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import type { UnknownAction } from '@reduxjs/toolkit'
import reducer, { captureForkOrigin, setActiveSlot, setSidePanelAlwaysOpen, toggleActivity, openActivityToTab, switchSlot } from '../store/chatSlice'
import './mockApiClient'
import { reduceResume } from './resumeLanded'

const initial = () => reducer(undefined, { type: '@@INIT' })
/** The user's setting turning on or off, on a chat page that allows it (the
 *  full page on a wide screen). */
const on = (value: boolean) => setSidePanelAlwaysOpen({ on: value, setting: value })
const switchTo = (arg: string) => ({
  type: 'chat/switchSlot/pending' as const,
  meta: { arg, requestId: `switch-${arg}`, requestStatus: 'pending' as const },
})
/** The failure of `switchTo(key)`. A 404 says the chat is gone, so the switch
 *  falls back to the chat it left. Any other status keeps `key` selected. */
const rejected = (key: string, status: number) =>
  switchSlot.rejected(new Error(String(status)), `switch-${key}`, key, { status } as never)
/** The success of a `createSlot` that started on state `from`. Its origin is
 *  what the real thunk captures there with `captureForkOrigin`, move counts
 *  included, so the create resolves as one the user stayed put for. */
const created = (key: string, from: ReturnType<typeof initial>) => {
  const origin = captureForkOrigin({ chat: from })
  return {
    type: 'chat/createSlot/fulfilled' as const,
    meta: {
      arg: undefined, requestId: `create-${key}`, requestStatus: 'fulfilled' as const,
      originActiveSlot: origin.activeSlot, originActiveSlotMoves: origin.moves, originActiveSlotAutoMoves: origin.autoMoves,
    },
    payload: { key },
  }
}
/** A resume of `key` that started on state `from` and took the view. */
const resumed = (from: ReturnType<typeof initial>, key: string) => reduceResume(
  reducer, from, { ok: true, key, nextBefore: 0, messages: [], hasMore: false, total: 0 }, `resume-${key}`, { key, title: key },
)

beforeEach(() => { localStorage.clear() })

describe('Keep side panel open', () => {
  it('enters a switched-to chat with its panel open, saving nothing for it', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, switchTo('B'))
    expect(s.activityOpen).toBe(true)
    expect(localStorage.getItem('mc-activity-open:B')).toBeNull()
  })

  it('enters a new chat and a resumed chat with the panel open', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, created('NEW', s))
    expect(s.activeSlot).toBe('NEW')
    expect(s.activityOpen).toBe(true)
    s = reducer(s, toggleActivity())
    s = resumed(s, 'OLD')
    expect(s.activityOpen).toBe(true)
  })

  it('opens the current chat when turned on', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    expect(s.activityOpen).toBe(false)
    s = reducer(s, on(true))
    expect(s.activityOpen).toBe(true)
  })

  it('does not reopen a panel the user closed when the same value arrives again', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, toggleActivity())
    expect(s.activityOpen).toBe(false)
    s = reducer(s, on(true))
    expect(s.activityOpen).toBe(false)
  })

  it('puts every chat back on its own saved choice when turned off', () => {
    localStorage.setItem('mc-activity-open:C', 'true')
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    // B and C are entered while it is on, and left without touching the panel.
    s = reducer(s, switchTo('B'))
    s = reducer(s, switchTo('C'))
    s = reducer(s, switchTo('A'))
    s = reducer(s, on(false))
    // A (current) and B had no saved choice; C saved "open".
    expect(s.activityOpen).toBe(false)
    expect(reducer(s, switchTo('B')).activityOpen).toBe(false)
    expect(reducer(s, switchTo('C')).activityOpen).toBe(true)
  })

  it('keeps a closed panel closed on a same-chat entry and on a rejected switch', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, toggleActivity())
    expect(s.activityOpen).toBe(false)
    // A return to the chat page re-switches to the chat already shown.
    s = reducer(s, switchTo('A'))
    expect(s.activityOpen).toBe(false)
    // A switch to a chat that is gone falls back to A.
    s = reducer(s, switchTo('GONE'))
    expect(s.activityOpen).toBe(true)
    s = reducer(s, switchSlot.rejected(new Error('404'), 'switch-GONE', 'GONE', { status: 404 } as never))
    expect(s.activeSlot).toBe('A')
    expect(s.activityOpen).toBe(false)
  })

  it('changes nothing when an off arrives and it was never on', () => {
    // A store built without the field (a partial state) with an open panel and
    // no saved choice: the chat page's mount-time off must leave it open.
    let s = { ...reducer(initial(), setActiveSlot('A')), activityOpen: true, sidePanelAlwaysOpen: undefined as unknown as boolean }
    s = reducer(s, on(false))
    expect(s.activityOpen).toBe(true)
  })

  it('does not open the current chat when the setting turned on while a phone held it off', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    // The setting turns on while a phone holds it off, then the screen widens:
    // the setting did not change on the way back to on.
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.sidePanelAlwaysOpen).toBe(true)
    expect(s.activityOpen).toBe(false)
  })

  it.each([
    ['a phone', { on: false, setting: true }],
    ['an embedded companion chat', { on: false }],
  ])('reopens a panel the setting opened once %s stops holding it off', (_, hold: { on: boolean; setting?: boolean }) => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen(hold))
    expect(s.activityOpen).toBe(false)
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.activityOpen).toBe(true)
    // Like the open it restores, the reopen saves nothing as A's choice.
    expect(localStorage.getItem('mc-activity-open:A')).toBeNull()
  })

  it('does not reopen it after the user acts on the panel during the hold', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    // On the phone the user opens a tab and closes the panel again.
    s = reducer(s, openActivityToTab('subagents'))
    s = reducer(s, toggleActivity())
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.activityOpen).toBe(false)
  })

  const holds: [string, { on: boolean; setting?: boolean }][] = [
    ['a phone', { on: false, setting: true }],
    ['an embedded companion chat', { on: false }],
  ]

  it.each(holds)('opens a chat entered during the hold once %s stops holding it off', (_, hold) => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen(hold))
    s = reducer(s, switchTo('B'))
    expect(s.activityOpen).toBe(false)
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.activeSlot).toBe('B')
    expect(s.activityOpen).toBe(true)
    expect(localStorage.getItem('mc-activity-open:B')).toBeNull()
  })

  it.each(holds)('reopens the chat it closed after a trip to another chat while %s holds it off', (_, hold) => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen(hold))
    s = reducer(s, switchTo('B'))
    s = reducer(s, switchTo('A'))
    expect(s.activityOpen).toBe(false)
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.activeSlot).toBe('A')
    expect(s.activityOpen).toBe(true)
  })

  it.each(holds)('reopens the chat it closed after a switch to a missing chat while %s holds it off', (_, hold) => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen(hold))
    // The user clicks a session the server no longer has, and the switch falls back to A.
    s = reducer(s, switchTo('GONE'))
    s = reducer(s, rejected('GONE', 404))
    expect(s.activeSlot).toBe('A')
    expect(s.activityOpen).toBe(false)
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.activityOpen).toBe(true)
    expect(localStorage.getItem('mc-activity-open:A')).toBeNull()
  })

  it('keeps the reopen cancelled after a switch to a missing chat when the user toggled the panel first', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    // On the phone the user opens A's panel and closes it again.
    s = reducer(s, toggleActivity())
    s = reducer(s, toggleActivity())
    s = reducer(s, switchTo('GONE'))
    s = reducer(s, rejected('GONE', 404))
    expect(s.activeSlot).toBe('A')
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.activityOpen).toBe(false)
  })

  const inFlightCancels: [string, UnknownAction][] = [
    ['a toggle of the panel', toggleActivity()],
    ['the setting turning off', setSidePanelAlwaysOpen({ on: false, setting: false })],
  ]

  it.each(inFlightCancels)('keeps the reopen cancelled when %s lands while a switch to a missing chat is in flight', (_, cancel) => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    s = reducer(s, switchTo('GONE'))
    s = reducer(s, cancel)
    s = reducer(s, rejected('GONE', 404))
    expect(s.activeSlot).toBe('A')
    // The phone reports the setting on again, then the screen widens.
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.activityOpen).toBe(false)
  })

  it.each(holds)('opens the chat it closed when %s stops holding it off while a switch to a missing chat is in flight', (_, hold) => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen(hold))
    s = reducer(s, switchTo('GONE'))
    // The hold lifts while the gone chat is still loading, then the switch falls back to A.
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    s = reducer(s, rejected('GONE', 404))
    expect(s.activeSlot).toBe('A')
    expect(s.activityOpen).toBe(true)
    expect(localStorage.getItem('mc-activity-open:A')).toBeNull()
  })

  it('keeps the reopen cancelled when the user toggles during a switch to a missing chat and the hold then lifts', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    s = reducer(s, switchTo('GONE'))
    // On the phone the user acts on the panel while the gone chat loads.
    s = reducer(s, toggleActivity())
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    s = reducer(s, rejected('GONE', 404))
    expect(s.activeSlot).toBe('A')
    expect(s.activityOpen).toBe(false)
  })

  it('keeps the reopen cancelled when the setting turns off and back on during a switch to a missing chat', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    s = reducer(s, switchTo('GONE'))
    // While the gone chat loads, the setting turns off, back on while the phone
    // still holds it, and then the screen widens.
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: false }))
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    s = reducer(s, rejected('GONE', 404))
    expect(s.activeSlot).toBe('A')
    expect(s.activityOpen).toBe(false)
  })

  it('opens a chat entered during the hold when its switch fails without a 404', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    // A transient failure keeps B selected, so B stays the chat to reopen.
    s = reducer(s, switchTo('B'))
    s = reducer(s, rejected('B', 500))
    expect(s.activeSlot).toBe('B')
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.activityOpen).toBe(true)
  })

  it('does not open a chat entered during the hold after the user toggles its panel', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    s = reducer(s, switchTo('B'))
    // On the phone the user opens B's panel and closes it again.
    s = reducer(s, toggleActivity())
    s = reducer(s, toggleActivity())
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.activeSlot).toBe('B')
    expect(s.activityOpen).toBe(false)
  })

  it('reopens nothing after the setting itself turns off', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, on(false))
    expect(s.activityOpen).toBe(false)
    // The setting turns on again while a phone holds it off, then the screen
    // widens: that turn-on opens nothing, as above.
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.activityOpen).toBe(false)
  })

  it('reopens nothing when the setting turns off during the hold', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: false }))
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.activityOpen).toBe(false)
  })

  it('does not reopen a closed panel when a phone or embed flip turns it back on', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, toggleActivity())
    // A phone, or an embedded chat, holds it off while the setting stays on.
    s = reducer(s, setSidePanelAlwaysOpen({ on: false, setting: true }))
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.sidePanelAlwaysOpen).toBe(true)
    expect(s.activityOpen).toBe(false)
  })

  it('keeps the setting it has when an embedded chat reports none', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(true))
    s = reducer(s, toggleActivity())
    // An embedded chat holds it off and sends no setting.
    s = reducer(s, setSidePanelAlwaysOpen({ on: false }))
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, setting: true }))
    expect(s.sidePanelAlwaysOpen).toBe(true)
    expect(s.activityOpen).toBe(false)
  })

  it('opens the chat on screen at the first report of a setting that is on', () => {
    // A reload: the chat was entered before the config read answered.
    let s = reducer(initial(), setActiveSlot('A'))
    expect(s.sidePanelAlwaysOpenSetting).toBeNull()
    s = reducer(s, on(true))
    expect(s.activityOpen).toBe(true)
    expect(localStorage.getItem('mc-activity-open:A')).toBeNull()
  })

  it('opens nothing at the first report after the user toggled the panel', () => {
    // The panel came back open from A's saved choice; the user closes it
    // before the config read answers.
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, toggleActivity())
    s = reducer(s, toggleActivity())
    s = reducer(s, on(true))
    expect(s.activityOpen).toBe(false)
  })

  it('leaves the panel to each chat while off', () => {
    localStorage.setItem('mc-activity-open:B', 'false')
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(false))
    s = reducer(s, switchTo('B'))
    expect(s.activityOpen).toBe(false)
    s = reducer(s, created('NEW', s))
    expect(s.activeSlot).toBe('NEW')
    expect(s.activityOpen).toBe(false)
  })
})
