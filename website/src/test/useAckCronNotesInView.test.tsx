// A cron run's bell note is read on two events only (#23564): the user opens
// its chat and a mounted pane then shows the transcript, or the note arrives
// live while a mounted pane shows it. Nothing is read from state alone: not a
// restored chat, not a loading or failed load, not a closed split pane, not a
// background tab, and never against a user's "Mark unread".
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { api } from '../api/client'
import chatReducer, { setActiveSlot, switchSlot } from '../store/chatSlice'
import notificationsReducer, { addNotification, unackNotification } from '../store/notificationsSlice'
import { dispatchLiveNotification } from '../hooks/notificationEvent'
import type { ChatMessage, Notification } from '../types'
import { OPEN_SETTLE_MS, OPEN_SETTLE_POLL_MS, transcriptShownFor, unreadCronNotesFor, useAckCronNotesInView, useMarkTranscriptShown } from '../hooks/useAckCronNotesInView'
import { useNavigate } from 'react-router-dom'
import { createTestStore, renderWithProviders } from './helpers'

const note = (over: Partial<Notification>): Notification => ({
  kind: 'cron', title: 'Nightly digest', body: 'done', ts: '2026-10-24T01:00:00Z', slot: 'cron-a', job_id: 'job-a',
  ...over,
})

const RUN = [{ role: 'assistant', content: 'Digest posted.', ts: 1, cls: '' }] as unknown as ChatMessage[]

type Store = ReturnType<typeof createTestStore>

/** A store with *items* in the bell and *activeSlot* holding a loaded transcript in Redux. */
function storeWith(items: Notification[], activeSlot: string | null = 'cron-a', chat: Record<string, unknown> = {}) {
  const base = { ...chatReducer(undefined, setActiveSlot(activeSlot as string)), messages: RUN, slotLoading: false, ...chat }
  const notifications = { ...notificationsReducer(undefined, { type: '@@test/init' }), items }
  return createTestStore({ chat: base, notifications } as never)
}

/** The user's open of *key* landing: `switchSlot.fulfilled` for a gesture. */
function landOpen(store: Store, key: string, arg: object = { announceOnMissing: true, gesture: true }) {
  act(() => {
    store.dispatch(switchSlot.fulfilled(
      { key, messages: RUN, running: false, stopping: false, hasMore: false, total: 1, queue: [] } as never,
      'req-open',
      { key, ...arg },
    ))
  })
}

/** A live `notification` frame, as useWebSocket handles one. */
function arrive(store: Store, n: Notification) {
  act(() => {
    store.dispatch(addNotification(n))
    dispatchLiveNotification(n)
  })
}

/** A chat pane. With *probe* it hands that element to the on-screen backstop,
 *  as the real panes hand their scroller; without, the backstop is skipped and
 *  only the marks decide (jsdom has no layout to measure). */
function Pane({ slot, shown, probe }: { slot: string; shown: boolean; probe?: Element }) {
  useMarkTranscriptShown(slot, shown, probe ? () => probe : undefined)
  return null
}

interface View { panes?: { slot: string; shown: boolean; probe?: Element }[]; hidden?: boolean }

/** The router's navigate(), so a test can leave the chat route and come back. */
let navTo: (to: string) => void = () => {}
function Nav() {
  const navigate = useNavigate()
  navTo = navigate
  return null
}

function Harness({ panes = [], hidden = false }: View) {
  useAckCronNotesInView({ hidden })
  return <><Nav />{panes.map((p, i) => <Pane key={i} slot={p.slot} shown={p.shown} probe={p.probe} />)}</>
}

function mount(store: Store, view: View = {}, route = '/chat') {
  const r = renderWithProviders(<Harness {...view} />, { store, route })
  return { ...r, update: (next: View) => r.rerender(<Harness {...next} />) }
}

const ackedTs = (store: Store) => store.getState().notifications.items.filter(n => n.acked).map(n => n.ts).sort()
const settle = () => act(async () => { await Promise.resolve() })

let ack: ReturnType<typeof vi.spyOn>
let focused = true
let docHidden = false
beforeEach(() => {
  focused = true
  docHidden = false
  ack = vi.spyOn(api, 'ackNotification').mockResolvedValue(new Response('{}'))
  vi.spyOn(api, 'unackNotification').mockResolvedValue(new Response('{}'))
  vi.spyOn(document, 'hasFocus').mockImplementation(() => focused)
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => docHidden)
})
afterEach(() => vi.restoreAllMocks())

const SHOWN_A = { panes: [{ slot: 'cron-a', shown: true }] }

describe('unreadCronNotesFor', () => {
  it('picks unread cron notes of the slot and nothing else', () => {
    const notes = [
      note({ ts: '1' }),
      note({ ts: '2', acked: true }),
      note({ ts: '3', slot: 'cron-b' }),
      note({ ts: '4', kind: 'approval' }),
      note({ ts: '5', kind: 'agent' }),
    ]
    expect(unreadCronNotesFor(notes, 'cron-a')).toEqual(['1'])
    expect(unreadCronNotesFor(notes, null)).toEqual([])
  })
})

describe('useMarkTranscriptShown', () => {
  it('counts a chat shown while any mounted pane shows it', () => {
    const r = mount(createTestStore(), { panes: [{ slot: 'cron-a', shown: true }, { slot: 'cron-a', shown: true }] })
    expect(transcriptShownFor('cron-a')).toBe(true)
    r.update({ panes: [{ slot: 'cron-a', shown: true }] })
    expect(transcriptShownFor('cron-a')).toBe(true)
    r.update({ panes: [{ slot: 'cron-a', shown: false }] })
    expect(transcriptShownFor('cron-a')).toBe(false)
    r.unmount()
    expect(transcriptShownFor('cron-a')).toBe(false)
  })
})

describe('switchSlot records the user open', () => {
  it('for a gesture only, not a plain, automatic or merely announced switch', () => {
    const store = storeWith([])
    landOpen(store, 'cron-a', {})
    landOpen(store, 'cron-a', { automatic: true })
    // An announced switch the page makes itself (a late ?sid= recovery, a
    // forwarded nav intent) is not the user opening the chat.
    landOpen(store, 'cron-a', { announceOnMissing: true })
    expect(store.getState().chat.userOpen).toBeUndefined()
    landOpen(store, 'cron-a')
    landOpen(store, 'cron-a')
    expect(store.getState().chat.userOpen).toEqual({ key: 'cron-a', seq: 2 })
  })
})

describe('useAckCronNotesInView', () => {
  it('opening the cron chat reads its notes, leaving every other note alone', async () => {
    const store = storeWith([
      note({ ts: 'run-1' }),
      note({ ts: 'run-2' }),
      note({ ts: 'other-session', slot: 'cron-b' }),
      note({ ts: 'approval', kind: 'approval' }),
    ])
    mount(store, SHOWN_A)
    landOpen(store, 'cron-a')
    await waitFor(() => expect(ackedTs(store)).toEqual(['run-1', 'run-2']))
    expect(ack.mock.calls.map(c => c[0]).sort()).toEqual(['run-1', 'run-2'])
  })

  it('reads nothing for a chat that is merely restored or already active', async () => {
    // A reload restores the chat with its transcript loaded: no one opened it.
    const store = storeWith([note({ ts: 'run-1' })])
    mount(store, SHOWN_A)
    await settle()
    expect(ack).not.toHaveBeenCalled()
  })

  it('waits for the transcript of an open still loading, and reads nothing for a load that failed', async () => {
    // Loading: the open's pane has not shown the transcript yet.
    const store = storeWith([note({ ts: 'run-1' })], 'cron-a', { messages: [], slotLoading: true })
    const r = mount(store, { panes: [{ slot: 'cron-a', shown: false }] })
    await settle()
    expect(ack).not.toHaveBeenCalled()
    landOpen(store, 'cron-a')
    await settle()
    expect(ack).not.toHaveBeenCalled()
    r.update(SHOWN_A)
    await waitFor(() => expect(ackedTs(store)).toEqual(['run-1']))
    r.unmount()

    // Failed: switchSlot.rejected lands no open, and the pane shows nothing.
    ack.mockClear()
    const failed = storeWith([note({ ts: 'run-2' })], 'cron-a', { messages: [], slotLoading: false })
    mount(failed, { panes: [{ slot: 'cron-a', shown: false }] })
    act(() => { failed.dispatch(switchSlot.rejected(new Error('boom'), 'req-open', { key: 'cron-a', announceOnMissing: true, gesture: true })) })
    arrive(failed, note({ ts: 'run-3' }))
    await settle()
    expect(ack).not.toHaveBeenCalled()
  })

  it('reads a note arriving live while a pane shows its chat', async () => {
    const store = storeWith([])
    mount(store, SHOWN_A)
    arrive(store, note({ ts: 'run-1' }))
    arrive(store, note({ ts: 'elsewhere', slot: 'cron-b' }))
    arrive(store, note({ ts: 'approval', kind: 'approval' }))
    await waitFor(() => expect(ackedTs(store)).toEqual(['run-1']))
    expect(ack.mock.calls.map(c => c[0])).toEqual(['run-1'])
  })

  it('leaves a note arriving for a split pane that was closed', async () => {
    // Closing the cron pane keeps its activeSlot and messages in Redux; only the
    // mounted panes say what is on screen.
    const store = storeWith([])
    const r = mount(store, { panes: [{ slot: 'cron-a', shown: true }, { slot: 'other', shown: true }] })
    r.update({ panes: [{ slot: 'other', shown: true }] })
    arrive(store, note({ ts: 'run-1' }))
    await settle()
    expect(ack).not.toHaveBeenCalled()
  })

  it('leaves a note arriving in a background tab unread, also once the tab comes back', async () => {
    const store = storeWith([])
    mount(store, SHOWN_A)
    docHidden = true
    arrive(store, note({ ts: 'run-1' }))
    await settle()
    docHidden = false
    act(() => { document.dispatchEvent(new Event('visibilitychange')); window.dispatchEvent(new Event('focus')) })
    await settle()
    expect(ack).not.toHaveBeenCalled()

    // An unfocused window counts as away too.
    focused = false
    arrive(store, note({ ts: 'run-2' }))
    await settle()
    expect(ack).not.toHaveBeenCalled()
  })

  it('never undoes a Mark unread, before or after a reload', async () => {
    const store = storeWith([note({ ts: 'run-1' })])
    const r = mount(store, SHOWN_A)
    landOpen(store, 'cron-a')
    await waitFor(() => expect(ackedTs(store)).toEqual(['run-1']))
    await act(async () => { await store.dispatch(unackNotification('run-1')) })
    await settle()
    expect(ackedTs(store)).toEqual([])
    r.unmount()

    // The reload: a fresh store, the same chat restored, the note unread.
    const reloaded = storeWith([note({ ts: 'run-1' })])
    mount(reloaded, SHOWN_A)
    await settle()
    expect(ackedTs(reloaded)).toEqual([])
    expect(ack.mock.calls.map(c => c[0])).toEqual(['run-1'])
  })

  it('keeps a Mark unread through every switch the page makes on its own', async () => {
    const store = storeWith([note({ ts: 'run-1' })])
    const r = mount(store, SHOWN_A)
    landOpen(store, 'cron-a')
    await waitFor(() => expect(ackedTs(store)).toEqual(['run-1']))
    await act(async () => { await store.dispatch(unackNotification('run-1')) })
    await settle()

    // With the chat on screen: the mount re-fetch or a URL switch (plain), the
    // auto-select (automatic), a late ?sid= recovery or a forwarded nav intent
    // (announced, no gesture).
    for (const arg of [{}, { automatic: true }, { announceOnMissing: true }]) {
      landOpen(store, 'cron-a', arg)
      await settle()
    }
    expect(ackedTs(store)).toEqual([])

    // A gesture open whose transcript the restore gate still hides, left before
    // the gate lifts: the pane never showed it.
    r.update({ panes: [{ slot: 'cron-a', shown: false }] })
    landOpen(store, 'cron-a')
    await settle()
    r.update({ panes: [{ slot: 'other', shown: true }] })
    await settle()
    expect(ackedTs(store)).toEqual([])
    expect(ack.mock.calls.map(c => c[0])).toEqual(['run-1'])
  })

  it('drops an open left for another page before its transcript shows, so a Mark unread survives the way back', async () => {
    // Opened from the sidebar, still loading; the user goes to the bell, marks
    // the run's note unread, and comes back to the chat.
    const store = storeWith([note({ ts: 'run-1', acked: true })], 'cron-a', { messages: [], slotLoading: true })
    const r = mount(store, { panes: [{ slot: 'cron-a', shown: false }] })
    landOpen(store, 'cron-a')
    act(() => navTo('/notifications'))
    await act(async () => { await store.dispatch(unackNotification('run-1')) })
    act(() => navTo('/chat'))
    r.update(SHOWN_A)
    await settle()
    expect(ackedTs(store)).toEqual([])
    expect(ack).not.toHaveBeenCalled()
  })

  it('drops an open left for another chat before its transcript shows, so the first chat is never read', async () => {
    const store = storeWith([note({ ts: 'run-1' })], 'cron-a', { messages: [], slotLoading: true })
    const r = mount(store, { panes: [{ slot: 'cron-a', shown: false }] })
    landOpen(store, 'cron-a')
    // Another chat before the first one showed, then back to it with no gesture
    // (a tab restore, Back): its pane showing it later reads nothing.
    act(() => { store.dispatch(setActiveSlot('cron-b')) })
    r.update({ panes: [{ slot: 'cron-b', shown: true }] })
    act(() => { store.dispatch(setActiveSlot('cron-a')) })
    r.update(SHOWN_A)
    await settle()
    expect(ack).not.toHaveBeenCalled()
  })

  it('does nothing away from a chat route, or while the Crew or a crew window covers the pane', async () => {
    const away = storeWith([])
    mount(away, SHOWN_A, '/notifications')
    arrive(away, note({ ts: 'run-1' }))

    const covered = storeWith([note({ ts: 'run-2' })])
    mount(covered, { ...SHOWN_A, hidden: true })
    landOpen(covered, 'cron-a')
    arrive(covered, note({ ts: 'run-3' }))
    await settle()
    expect(ack).not.toHaveBeenCalled()
  })

  it('asks once per event when the server refuses, instead of looping', async () => {
    ack.mockRejectedValue(new Error('nope'))
    const store = storeWith([])
    mount(store, SHOWN_A)
    arrive(store, note({ ts: 'run-1' }))
    await waitFor(() => expect(ack).toHaveBeenCalledTimes(1))
    await settle()
    expect(ack).toHaveBeenCalledTimes(1)
  })
})

// The invariant, as a matrix (#23564): a cron note is read only when the user
// opened its chat (or it arrived live) AND the transcript is on screen. Each row
// is one thing that can hide or replace the pane, run through both events; the
// controls are things that look similar but leave the transcript visible.
describe('the pane-visible matrix', () => {
  type Row = {
    name: string
    read: boolean
    /** Sets the scene up before the event; returns its own undo. */
    arrange?: () => (() => void) | void
    view?: View
    route?: string
  }
  const addEl = (attrs: Record<string, string>, parent: Element = document.body) => {
    const el = document.createElement('div')
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v)
    parent.appendChild(el)
    return () => el.remove()
  }
  const rows: Row[] = [
    { name: 'the transcript on screen, nothing over it', read: true },
    { name: 'a modal dialog: Modal, the Radix dialog, a side sheet, the shortcuts or update dialog',
      read: false, arrange: () => addEl({ role: 'dialog', 'aria-modal': 'true' }) },
    { name: 'the command palette or the command bar (modal dialogs)',
      read: false, arrange: () => addEl({ role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Command palette' }) },
    { name: 'a confirm (modal alertdialog)', read: false, arrange: () => addEl({ role: 'alertdialog', 'aria-modal': 'true' }) },
    { name: 'the phone nav drawer or sessions drawer (data-covers-pane)', read: false, arrange: () => addEl({ 'data-covers-pane': '' }) },
    { name: 'the phone side panel open: Activity, Workflows, Search (data-covers-pane)', read: false,
      arrange: () => addEl({ 'data-covers-pane': '', 'data-testid': 'side-panel-overlay' }) },
    { name: 'an MCP app promoted to the overlay, or the crew editor loading scrim (data-covers-pane)', read: false,
      arrange: () => addEl({ 'data-covers-pane': '', role: 'dialog' }) },
    { name: 'a background tab or a minimized window (document.hidden)', read: false, arrange: () => { docHidden = true } },
    { name: 'another application in front (no focus)', read: false, arrange: () => { focused = false } },
    { name: 'a cover only the bell knows: a remote Crew, a crew window, its detail card', read: false, view: { ...SHOWN_A, hidden: true } },
    { name: 'another page (settings, the bell page)', read: false, route: '/settings' },
    { name: 'a background session tab, a closed split pane or a popout (no pane here shows it)',
      read: false, view: { panes: [{ slot: 'other', shown: true }] } },
    // Controls.
    { name: 'control: a non-modal popover', read: true, arrange: () => addEl({ role: 'dialog' }) },
    // A drawer sliding out after the tap that opened the chat stays mounted
    // but drops its mark (OverlayDrawer `coversPane`, the nav rail's open phase).
    { name: 'control: a phone drawer sliding out, mounted but no longer marked', read: true, arrange: () => addEl({ 'data-drawer': 'closing' }) },
    { name: 'control: the guide\'s own layer', read: true, arrange: () => {
      const layer = document.createElement('div')
      layer.setAttribute('data-guide-layer', '')
      document.body.appendChild(layer)
      addEl({ role: 'dialog', 'aria-modal': 'true' }, layer)
      return () => layer.remove()
    } },
    { name: 'control: split view, the cron chat in the pane that is not focused', read: true,
      view: { panes: [{ slot: 'other', shown: true }, { slot: 'cron-a', shown: true }] } },
  ]
  const events = [
    { event: 'a gesture open', run: (store: Store) => landOpen(store, 'cron-a'), notes: [note({ ts: 'run-1' })] },
    { event: 'a live arrival', run: (store: Store) => arrive(store, note({ ts: 'run-1' })), notes: [] as Notification[] },
  ]
  const cases = rows.flatMap(row => events.map(e => ({ ...row, ...e })))

  it.each(cases)('$event, $name: read=$read', async ({ arrange, view, route, read, run, notes }) => {
    const store = storeWith(notes)
    mount(store, view ?? SHOWN_A, route ?? '/chat')
    const undo = arrange?.()
    try {
      run(store)
      if (read) await waitFor(() => expect(ackedTs(store)).toEqual(['run-1']))
      else {
        await settle()
        expect(ack).not.toHaveBeenCalled()
      }
    } finally {
      if (typeof undo === 'function') undo()
    }
  })
})

// The on-screen backstop (#23564): with no mark and no modal, a note is still
// read only when the chat's transcript element is actually on screen. jsdom
// lays nothing out, so each row stubs the transcript's box and the hit test:
// the viewport is jsdom's 1024x768.
describe('the on-screen backstop', () => {
  type Box = { left: number; top: number; width: number; height: number }
  const ON_SCREEN: Box = { left: 300, top: 60, width: 600, height: 600 }
  type Row = { name: string; read: boolean; box: Box; hit: 'transcript' | 'overlay' | 'none' }
  const rows: Row[] = [
    { name: 'on screen, the hit test lands in it', read: true, box: ON_SCREEN, hit: 'transcript' },
    { name: 'squeezed to zero width (a panel fills the column)', read: false, box: { ...ON_SCREEN, width: 0 }, hit: 'transcript' },
    { name: 'zero height, or display: none', read: false, box: { ...ON_SCREEN, height: 0 }, hit: 'transcript' },
    { name: 'scrolled or slid off screen', read: false, box: { ...ON_SCREEN, left: 1100 }, hit: 'transcript' },
    { name: 'an unmarked opaque overlay drawn over it', read: false, box: ON_SCREEN, hit: 'overlay' },
    { name: 'nothing hit at all', read: false, box: ON_SCREEN, hit: 'none' },
  ]

  let transcript: HTMLElement
  let overlay: HTMLElement
  let hitMode: Row['hit'] = 'transcript'
  beforeEach(() => {
    transcript = document.createElement('div')
    transcript.appendChild(document.createElement('p'))
    overlay = document.createElement('div')
    document.body.append(transcript, overlay)
    // jsdom has no elementFromPoint: give it one, answering from the row's scene.
    Object.defineProperty(document, 'elementFromPoint', {
      configurable: true,
      value: () => (hitMode === 'transcript' ? transcript.firstElementChild : hitMode === 'overlay' ? overlay : null),
    })
  })
  afterEach(() => {
    transcript.remove()
    overlay.remove()
    delete (document as { elementFromPoint?: unknown }).elementFromPoint
  })
  const stubBox = (b: Box) => vi.spyOn(transcript, 'getBoundingClientRect').mockReturnValue(
    { ...b, right: b.left + b.width, bottom: b.top + b.height, x: b.left, y: b.top, toJSON: () => ({}) } as DOMRect,
  )
  const events = [
    { event: 'a gesture open', run: (store: Store) => landOpen(store, 'cron-a'), notes: [note({ ts: 'run-1' })] },
    { event: 'a live arrival', run: (store: Store) => arrive(store, note({ ts: 'run-1' })), notes: [] as Notification[] },
  ]
  const cases = rows.flatMap(row => events.map(e => ({ ...row, ...e })))

  it.each(cases)('$event, transcript $name: read=$read', async ({ box, hit, read, run, notes }) => {
    stubBox(box)
    hitMode = hit
    const store = storeWith(notes)
    mount(store, { panes: [{ slot: 'cron-a', shown: true, probe: transcript }] })
    run(store)
    if (read) await waitFor(() => expect(ackedTs(store)).toEqual(['run-1']))
    else {
      await settle()
      expect(ack).not.toHaveBeenCalled()
    }
  })

  it('reads when one of two split panes showing the chat is on screen', async () => {
    const hidden = document.createElement('div')
    document.body.appendChild(hidden)
    vi.spyOn(hidden, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect)
    stubBox(ON_SCREEN)
    hitMode = 'transcript'
    try {
      const store = storeWith([])
      mount(store, { panes: [{ slot: 'cron-a', shown: true, probe: hidden }, { slot: 'cron-a', shown: true, probe: transcript }] })
      arrive(store, note({ ts: 'run-1' }))
      await waitFor(() => expect(ackedTs(store)).toEqual(['run-1']))
    } finally {
      hidden.remove()
    }
  })
})

// A gesture open often takes its own cover away (#23564): a tap on a session row
// in the phone drawer opens the chat and closes the drawer, whose scrim is still
// on screen while it slides out. The open waits up to OPEN_SETTLE_MS for that.
describe('a gesture open waits for the cover its gesture is removing', () => {
  const coverOn = () => {
    const el = document.createElement('div')
    el.setAttribute('data-covers-pane', '')
    document.body.appendChild(el)
    return () => el.remove()
  }
  beforeEach(() => { vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] }) })
  afterEach(() => { vi.useRealTimers() })
  const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

  it('reads once a closing drawer has gone, within the window', async () => {
    const store = storeWith([note({ ts: 'run-1' })])
    mount(store, SHOWN_A)
    const coverOff = coverOn()
    landOpen(store, 'cron-a')
    await advance(OPEN_SETTLE_POLL_MS)
    expect(ack).not.toHaveBeenCalled()
    // The drawer's slide-out ends (0.45s at most).
    coverOff()
    await advance(OPEN_SETTLE_POLL_MS)
    expect(ack).toHaveBeenCalledWith('run-1')
  })

  it('drops the open for good when the cover outlasts the window', async () => {
    const store = storeWith([note({ ts: 'run-1' })])
    mount(store, SHOWN_A)
    const coverOff = coverOn()
    landOpen(store, 'cron-a')
    await advance(OPEN_SETTLE_MS + OPEN_SETTLE_POLL_MS)
    coverOff()
    await advance(OPEN_SETTLE_MS)
    expect(ack).not.toHaveBeenCalled()
  })

  it('drops the open at once when the user leaves the chat inside the window', async () => {
    const store = storeWith([note({ ts: 'run-1' })])
    mount(store, SHOWN_A)
    const coverOff = coverOn()
    landOpen(store, 'cron-a')
    await advance(OPEN_SETTLE_POLL_MS)
    act(() => navTo('/notifications'))
    coverOff()
    act(() => navTo('/chat'))
    await advance(OPEN_SETTLE_MS)
    expect(ack).not.toHaveBeenCalled()
  })
})
