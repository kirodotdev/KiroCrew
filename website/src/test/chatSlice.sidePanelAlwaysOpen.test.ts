/**
 * Keep side panel open (`dashboard.side_panel_always_open`) in the chat slice:
 * while on, every slot-entry path enters with the panel open, and none of them
 * saves that as the chat's own choice; turning it off puts every chat back on
 * its saved choice.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import reducer, { setActiveSlot, setSidePanelAlwaysOpen, toggleActivity, resumeFromHistory, switchSlot } from '../store/chatSlice'
import './mockApiClient'

const initial = () => reducer(undefined, { type: '@@INIT' })
/** The user's setting turning on or off (`openCurrent` when it turns on). */
const on = (value: boolean) => setSidePanelAlwaysOpen({ on: value, openCurrent: value })
const switchTo = (arg: string) => ({
  type: 'chat/switchSlot/pending' as const,
  meta: { arg, requestId: `switch-${arg}`, requestStatus: 'pending' as const },
})
const created = (key: string, origin: string) => ({
  type: 'chat/createSlot/fulfilled' as const,
  meta: { arg: undefined, requestId: `create-${key}`, requestStatus: 'fulfilled' as const, originActiveSlot: origin },
  payload: { key },
})
const resumed = (key: string) => resumeFromHistory.fulfilled(
  { ok: true, key, nextBefore: 0, messages: [], hasMore: false, total: 0 },
  `resume-${key}`,
  { key, title: key },
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
    s = reducer(s, created('NEW', 'A'))
    expect(s.activeSlot).toBe('NEW')
    expect(s.activityOpen).toBe(true)
    s = reducer(s, toggleActivity())
    s = reducer(s, resumed('OLD'))
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

  it('opens the current chat only when the setting itself turns on', () => {
    let s = reducer(initial(), setActiveSlot('A'))
    // A phone or embed flip back to on (the setting did not change).
    s = reducer(s, setSidePanelAlwaysOpen({ on: true, openCurrent: false }))
    expect(s.sidePanelAlwaysOpen).toBe(true)
    expect(s.activityOpen).toBe(false)
  })

  it('leaves the panel to each chat while off', () => {
    localStorage.setItem('mc-activity-open:B', 'false')
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, on(false))
    s = reducer(s, switchTo('B'))
    expect(s.activityOpen).toBe(false)
    s = reducer(s, created('NEW', 'B'))
    expect(s.activityOpen).toBe(false)
  })
})
