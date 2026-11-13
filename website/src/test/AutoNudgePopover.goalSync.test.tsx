import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { TAB_ID } from '../api/tabId'
import {
  GOAL_DRAFTS_KEY,
  GOAL_DRAFT_SYNC_TIMEOUT_MS,
  LEGACY_GOAL_DRAFTS_KEY,
  __resetForTests,
  loadLegacyGoalDraft,
  loadPendingGoalDraft,
} from '../utils/goalDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'
import {
  SLOT, deferred, goalBox, makeLoop, renderPopover, stubGoalGateway, wireDraft,
} from './autoNudgePopoverFixtures'

// Each pending record names the tab that wrote it; a fixed id keeps its checks exact.
vi.mock('../api/tabId', () => ({ TAB_ID: 'tab-under-test' }))

const startButton = () => screen.getByRole('button', { name: /Start loop/i })
const status = () => screen.getByTestId('goal-draft-sync-status')
const syncError = () => screen.queryByTestId('goal-draft-sync-error')
const checkDone = () => waitFor(() => expect(status()).toBeEmptyDOMElement())

describe('AutoNudgePopover goal persistence and cross-device sync', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

  it('remembers the user-typed goal on the server and restores it after the loop is gone (the reported bug)', async () => {
    const gateway = stubGoalGateway()
    const first = renderPopover(null)
    await checkDone()
    fireEvent.change(goalBox(), { target: { value: 'Ship the BYOA gate harness' } })
    await waitFor(() => expect(gateway.row.draft?.message).toBe('Ship the BYOA gate harness'))
    expect(gateway.puts[0]).toMatchObject({ base_updated_at: 0 })
    // The server confirmed it, so nothing is left pending in this browser.
    expect(loadPendingGoalDraft(SLOT)).toBeNull()
    first.unmount()

    renderPopover(null)
    await waitFor(() => expect(goalBox().value).toBe('Ship the BYOA gate harness'))
  })

  it('keeps the form read-only and Start disabled while the open-time check runs', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('saved goal'), updated_at: 5 })
    const read = deferred<void>()
    gateway.intercept = (url, init) => (
      url.startsWith('/api/autonudge/draft/slot/') && !init?.method
        ? read.promise.then(() => ({ ok: true, status: 200, json: () => Promise.resolve(gateway.row) }))
        : undefined
    )
    renderPopover(null)

    expect(status()).toHaveTextContent('Checking saved goal')
    expect(goalBox()).toBeDisabled()
    expect(screen.getByRole('spinbutton', { name: /Seconds between nudges/i })).toBeDisabled()
    expect(startButton()).toBeDisabled()

    await act(async () => { read.resolve() })
    await checkDone()
    expect(goalBox()).toBeEnabled()
    expect(goalBox().value).toBe('saved goal')
    expect(startButton()).toBeEnabled()
    expect(gateway.puts).toHaveLength(0)
  })

  it('never starts unsaved text: Start stays disabled through the debounce and the save, then runs the confirmed text', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('saved goal'), updated_at: 5 })
    const put = deferred<void>()
    gateway.intercept = (url, init) => {
      if (!url.startsWith('/api/autonudge/draft/slot/') || init?.method !== 'PUT') return undefined
      const body = JSON.parse(String(init.body))
      gateway.puts.push(body)
      return put.promise.then(() => gateway.apply(body))
    }
    renderPopover(null)
    await checkDone()
    expect(startButton()).toBeEnabled()

    vi.useFakeTimers({ shouldAdvanceTime: true })
    fireEvent.change(goalBox(), { target: { value: 'edited goal' } })
    // Inside the debounce: nothing sent yet, and the unsaved text cannot start.
    expect(gateway.puts).toHaveLength(0)
    expect(startButton()).toBeDisabled()
    // Start's row names the save it waits for, never an empty line.
    expect(status()).toHaveTextContent(/Saving/)
    expect(startButton()).toHaveAttribute('aria-describedby', status().id)
    await act(async () => { await vi.advanceTimersByTimeAsync(DRAFT_SAVE_DEBOUNCE_MS) })
    expect(gateway.puts).toHaveLength(1)
    expect(status()).toHaveTextContent(/Saving/)
    expect(startButton()).toBeDisabled()
    // The editor stays editable while a save runs.
    expect(goalBox()).toBeEnabled()

    await act(async () => { put.resolve() })
    await waitFor(() => expect(startButton()).toBeEnabled())
    const confirmedAt = gateway.row.updated_at
    await act(async () => { fireEvent.click(startButton()) })
    await waitFor(() => expect(gateway.creates).toHaveLength(1))
    expect(gateway.creates[0]).toMatchObject({ message: 'edited goal', draft_updated_at: confirmedAt })
  })

  it('stores text typed during an unanswered save here at once, and only a confirmed save of that text drops it', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('saved goal'), updated_at: 5 })
    const firstSave = deferred<void>()
    gateway.intercept = (url, init) => {
      if (!url.startsWith('/api/autonudge/draft/slot/') || init?.method !== 'PUT' || gateway.puts.length > 0) return undefined
      const body = JSON.parse(String(init.body))
      gateway.puts.push(body)
      return firstSave.promise.then(() => gateway.apply(body))
    }
    renderPopover(null)
    await checkDone()
    fireEvent.change(goalBox(), { target: { value: 'goal A' } })
    await waitFor(() => expect(gateway.puts).toHaveLength(1))

    // Typed while A's save is unanswered. A reload runs no close flush, so the
    // edit must already be in this browser's storage.
    fireEvent.change(goalBox(), { target: { value: 'goal B' } })
    const typed = { message: 'goal B', idleSecs: 60, maxCycles: 0 }
    expect(JSON.parse(localStorage.getItem(GOAL_DRAFTS_KEY) ?? '{}')[SLOT]).toEqual({ draft: typed, base: 5, tab: TAB_ID })

    // A's confirmed save is not the editor's text: B stays, now on A's version.
    await act(async () => { firstSave.resolve() })
    await waitFor(() => expect(loadPendingGoalDraft(SLOT)).toEqual({ draft: typed, base: 1001, tab: TAB_ID }))
    await waitFor(() => expect(loadPendingGoalDraft(SLOT)).toBeNull())
    expect(gateway.puts).toEqual([
      { base_updated_at: 5, draft: wireDraft('goal A') },
      { base_updated_at: 1001, draft: wireDraft('goal B') },
    ])
    expect(gateway.row.draft).toEqual(wireDraft('goal B'))
  })

  it('a save confirmed in another tab leaves this tab\'s edit on its own version, so its next save is a conflict', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('saved goal'), updated_at: 5 })
    const otherTabSave = deferred<void>()
    gateway.intercept = (url, init) => {
      if (init?.method !== 'PUT' || !String(init.body).includes('goal from tab A')) return undefined
      const body = JSON.parse(String(init.body))
      gateway.puts.push(body)
      return otherTabSave.promise.then(() => gateway.apply(body))
    }
    renderPopover(null)
    await checkDone()
    // Tab A: the same browser storage, its own copy of the goal-draft module, on version 5.
    vi.resetModules()
    vi.doMock('../api/tabId', () => ({ TAB_ID: 'tab-A' }))
    const tabA = await import('../utils/goalDrafts')
    const goalA = { message: 'goal from tab A', idleSecs: 60, maxCycles: 0 }
    tabA.adoptGoalDraft(SLOT, { draft: { message: 'saved goal', idleSecs: 60, maxCycles: 0 }, updatedAt: 5 })
    tabA.savePendingGoalDraft(SLOT, goalA)
    const savedInA = tabA.syncGoalDraft(SLOT, goalA)
    await waitFor(() => expect(gateway.puts).toHaveLength(1))

    // This tab types, and A's save is confirmed inside this tab's debounce window.
    fireEvent.change(goalBox(), { target: { value: 'goal from tab B' } })
    await act(async () => {
      otherTabSave.resolve()
      await savedInA
    })
    expect(loadPendingGoalDraft(SLOT)).toEqual({ draft: { message: 'goal from tab B', idleSecs: 60, maxCycles: 0 }, base: 5, tab: TAB_ID })

    // The next save goes out on the version this tab loaded and meets A's goal as a conflict.
    await waitFor(() => expect(syncError()).toHaveTextContent('A newer goal was saved on another device'))
    expect(screen.getByRole('button', { name: 'Use newer goal' })).toBeEnabled()
    expect(gateway.puts[1]).toEqual({ base_updated_at: 5, draft: wireDraft('goal from tab B') })
    expect(gateway.row.draft).toEqual(wireDraft('goal from tab A'))
  })

  /** Tab A (the same browser storage, its own copy of the goal-draft module) holds
   *  an unsaved edit made on version 5 while this tab saves and confirms a newer
   *  goal. Reopened here, the check restores A's edit and meets that newer goal. */
  async function reopenOverTabAEdit(gateway: ReturnType<typeof stubGoalGateway>) {
    const first = renderPopover(null)
    await checkDone()
    vi.resetModules()
    vi.doMock('../api/tabId', () => ({ TAB_ID: 'tab-A' }))
    const tabA = await import('../utils/goalDrafts')
    tabA.adoptGoalDraft(SLOT, { draft: { message: 'saved goal', idleSecs: 60, maxCycles: 0 }, updatedAt: 5 })
    fireEvent.change(goalBox(), { target: { value: 'newer goal' } })
    await waitFor(() => expect(gateway.row.draft).toEqual(wireDraft('newer goal')))
    await waitFor(() => expect(loadPendingGoalDraft(SLOT)).toBeNull())
    first.unmount()
    tabA.savePendingGoalDraft(SLOT, { message: 'goal from tab A', idleSecs: 60, maxCycles: 0 })

    renderPopover(null)
    await waitFor(() => expect(syncError()).toHaveTextContent('A newer goal was saved on another device'))
    expect(goalBox().value).toBe('goal from tab A')
    return tabA
  }

  it('text typed over another tab\'s restored edit is saved on that edit\'s version and meets the conflict notice', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('saved goal'), updated_at: 5 })
    await reopenOverTabAEdit(gateway)

    fireEvent.change(goalBox(), { target: { value: 'typed over the restored edit' } })
    await waitFor(() => expect(gateway.puts).toHaveLength(3))
    expect(gateway.puts.slice(1)).toEqual([
      { base_updated_at: 5, draft: wireDraft('goal from tab A') },
      { base_updated_at: 5, draft: wireDraft('typed over the restored edit') },
    ])
    expect(syncError()).toHaveTextContent('A newer goal was saved on another device')
    expect(gateway.row.draft).toEqual(wireDraft('newer goal'))
    expect(loadPendingGoalDraft(SLOT)).toEqual({ draft: { message: 'typed over the restored edit', idleSecs: 60, maxCycles: 0 }, base: 5, tab: TAB_ID })
  })

  it('the tab that wrote a restored edit keeps saving it on its own version after this tab takes it over', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('saved goal'), updated_at: 5 })
    const tabA = await reopenOverTabAEdit(gateway)
    // This tab holds A's edit as its own now, on the version A made it on.
    expect(loadPendingGoalDraft(SLOT)).toEqual({ draft: { message: 'goal from tab A', idleSecs: 60, maxCycles: 0 }, base: 5, tab: TAB_ID })

    // Tab A keeps typing: its save goes out on the version A loaded and meets the newer goal too.
    const typedInA = { message: 'tab A keeps typing', idleSecs: 60, maxCycles: 0 }
    tabA.savePendingGoalDraft(SLOT, typedInA)
    await expect(tabA.syncGoalDraft(SLOT, typedInA)).resolves.toMatchObject({ result: 'conflict' })
    expect(gateway.puts.at(-1)).toEqual({ base_updated_at: 5, draft: wireDraft('tab A keeps typing') })
    expect(gateway.row.draft).toEqual(wireDraft('newer goal'))
  })

  it('opening over a stored goal never writes, and shows another device\'s newer goal as Updated', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('goal A'), updated_at: 5 })
    const first = renderPopover(null)
    await waitFor(() => expect(goalBox().value).toBe('goal A'))
    expect(status()).toBeEmptyDOMElement()
    first.unmount()
    gateway.row = { draft: wireDraft('goal B from the phone', 120, 4), updated_at: 9 }

    renderPopover(null)

    await waitFor(() => expect(status()).toHaveTextContent('Updated from another device.'))
    expect(goalBox().value).toBe('goal B from the phone')
    expect(screen.getByDisplayValue('120')).toBeInTheDocument()
    expect(startButton()).toBeEnabled()
    expect(gateway.puts).toHaveLength(0)
    expect(loadPendingGoalDraft(SLOT)).toBeNull()
  })

  it('uploads a pre-sync browser draft into an empty server slot once, then deletes it', async () => {
    localStorage.setItem(LEGACY_GOAL_DRAFTS_KEY, JSON.stringify({ [SLOT]: { message: 'old browser goal', idleSecs: 90, maxCycles: 2 } }))
    const gateway = stubGoalGateway()
    const first = renderPopover(null)

    await waitFor(() => expect(gateway.row.draft).toEqual(wireDraft('old browser goal', 90, 2)))
    expect(gateway.puts).toEqual([{ base_updated_at: 0, draft: wireDraft('old browser goal', 90, 2) }])
    expect(loadLegacyGoalDraft(SLOT)).toBeNull()
    expect(loadPendingGoalDraft(SLOT)).toBeNull()
    await waitFor(() => expect(startButton()).toBeEnabled())
    first.unmount()

    renderPopover(null)
    await checkDone()
    expect(gateway.puts).toHaveLength(1)
  })

  it('drops a pre-sync browser draft without uploading it when the server slot already holds a record', async () => {
    localStorage.setItem(LEGACY_GOAL_DRAFTS_KEY, JSON.stringify({ [SLOT]: { message: 'old browser goal', idleSecs: 90, maxCycles: 2 } }))
    const gateway = stubGoalGateway({ draft: wireDraft('server goal'), updated_at: 5 })
    renderPopover(null)

    await waitFor(() => expect(goalBox().value).toBe('server goal'))
    expect(gateway.puts).toHaveLength(0)
    expect(loadLegacyGoalDraft(SLOT)).toBeNull()
    expect(syncError()).toBeNull()
  })

  it('a failed save keeps the edit pending with Retry, and Retry saves it on the version it was made on', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('saved goal'), updated_at: 5 })
    let offline = true
    gateway.intercept = (url, init) => (
      offline && url.startsWith('/api/autonudge/draft/slot/') && init?.method === 'PUT'
        ? Promise.reject(new TypeError('Failed to fetch'))
        : undefined
    )
    renderPopover(null)
    await checkDone()
    fireEvent.change(goalBox(), { target: { value: 'offline edit' } })

    await waitFor(() => expect(syncError()).toHaveTextContent(/Retry to sync/))
    expect(startButton()).toBeDisabled()
    expect(goalBox().value).toBe('offline edit')
    expect(loadPendingGoalDraft(SLOT)).toEqual({ draft: { message: 'offline edit', idleSecs: 60, maxCycles: 0 }, base: 5, tab: TAB_ID })

    offline = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(startButton()).toBeEnabled())
    expect(gateway.puts).toEqual([{ base_updated_at: 5, draft: wireDraft('offline edit') }])
    expect(gateway.row.draft).toEqual(wireDraft('offline edit'))
    expect(loadPendingGoalDraft(SLOT)).toBeNull()
  })

  it('an edit this browser refuses to store and the server misses is kept on screen, and Retry sends it', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('saved goal'), updated_at: 5 })
    let offline = true
    gateway.intercept = (url, init) => (
      offline && url.startsWith('/api/autonudge/draft/slot/') && init?.method === 'PUT'
        ? Promise.reject(new TypeError('Failed to fetch'))
        : undefined
    )
    const setItem = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === GOAL_DRAFTS_KEY) throw new DOMException('denied', 'SecurityError')
      setItem.call(this, key, value)
    })
    renderPopover(null)
    await checkDone()
    fireEvent.change(goalBox(), { target: { value: 'unstored edit' } })
    await waitFor(() => expect(syncError()).toHaveTextContent(/Retry to sync/))
    expect(localStorage.getItem(GOAL_DRAFTS_KEY)).toBeNull()

    offline = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(startButton()).toBeEnabled())
    expect(goalBox().value).toBe('unstored edit')
    expect(gateway.puts).toEqual([{ base_updated_at: 5, draft: wireDraft('unstored edit') }])
    expect(loadPendingGoalDraft(SLOT)).toBeNull()
  })

  it('a failed read shows Retry, keeps the pending edit, and saves it once the store answers', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('saved goal'), updated_at: 5 })
    let down = true
    gateway.intercept = url => (down && url.startsWith('/api/autonudge/draft/slot/')
      ? Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({ error: 'goal draft storage is unavailable' }) })
      : undefined)
    // An edit an earlier visit could not sync.
    localStorage.setItem(GOAL_DRAFTS_KEY, JSON.stringify({ [SLOT]: { draft: { message: 'unsynced edit', idleSecs: 60, maxCycles: 0 }, base: 5 } }))
    renderPopover(null)

    await waitFor(() => expect(syncError()).toHaveTextContent(/Retry to sync/))
    expect(goalBox().value).toBe('unsynced edit')
    expect(startButton()).toBeDisabled()

    down = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(startButton()).toBeEnabled())
    expect(gateway.row.draft).toEqual(wireDraft('unsynced edit'))
  })

  it('a draft GET whose body stalls after its headers ends at the deadline in the failed check with Retry', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const gateway = stubGoalGateway()
    gateway.intercept = (url, init) => (url.startsWith('/api/autonudge/draft/slot/') && !init?.method
      ? Promise.resolve({ ok: true, status: 200, json: () => new Promise(() => {}) })
      : undefined)
    renderPopover(null)
    expect(status()).toHaveTextContent('Checking saved goal')

    await act(async () => { await vi.advanceTimersByTimeAsync(GOAL_DRAFT_SYNC_TIMEOUT_MS + 1) })

    await waitFor(() => expect(syncError()).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(goalBox()).toBeEnabled()
    expect(startButton()).toBeDisabled()
  })

  it('reopening while the close flush still saves reads the record that save confirms, not a conflict', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('saved goal'), updated_at: 5 })
    const put = deferred<void>()
    gateway.intercept = (url, init) => {
      if (!url.startsWith('/api/autonudge/draft/slot/') || init?.method !== 'PUT') return undefined
      const body = JSON.parse(String(init.body))
      gateway.puts.push(body)
      return put.promise.then(() => gateway.apply(body))
    }
    const first = renderPopover(null)
    await checkDone()
    fireEvent.change(goalBox(), { target: { value: 'closed fast' } })
    first.unmount()

    renderPopover(null)
    await waitFor(() => expect(gateway.puts).toEqual([{ base_updated_at: 5, draft: wireDraft('closed fast') }]))
    expect(status()).toHaveTextContent('Checking saved goal')
    await act(async () => { put.resolve() })

    await waitFor(() => expect(startButton()).toBeEnabled())
    expect(goalBox().value).toBe('closed fast')
    expect(syncError()).toBeNull()
    expect(gateway.puts).toHaveLength(1)
  })

  it('does not persist the pristine default (an untouched popover pins nothing, on open or close)', async () => {
    const gateway = stubGoalGateway()
    const view = renderPopover(null)
    await checkDone()
    view.unmount()
    expect(gateway.puts).toHaveLength(0)
    expect(loadPendingGoalDraft(SLOT)).toBeNull()
  })

  it('keeps the status line mounted through checking and idle so the form does not shift', async () => {
    stubGoalGateway()
    renderPopover(null)
    const line = status()
    expect(line).toHaveTextContent('Checking saved goal')
    await checkDone()
    expect(status()).toBe(line)
    expect(startButton()).not.toHaveAttribute('aria-describedby')
  })

  it('a live loop is authoritative: no sync status, no draft read, no draft write', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('stale draft goal'), updated_at: 5 })
    vi.useFakeTimers()
    const view = renderPopover(makeLoop({ message: 'active loop goal' }))
    expect(goalBox().value).toBe('active loop goal')
    expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull()
    fireEvent.change(goalBox(), { target: { value: 'tweaked while running' } })
    act(() => { vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS) })
    view.unmount()
    expect(gateway.puts).toHaveLength(0)
    expect(loadPendingGoalDraft(SLOT)).toBeNull()
  })

  it('falsy loop fields fall back to default template / 60 / 0, not bare "" / 0 (|| not ??)', () => {
    stubGoalGateway()
    renderPopover(makeLoop({ message: '', idle_secs: 0, max_cycles: 0 }))
    expect(goalBox().value).toContain('north star')
    expect((screen.getByDisplayValue('60') as HTMLInputElement).value).toBe('60')
  })
})
