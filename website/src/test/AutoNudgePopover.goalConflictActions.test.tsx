import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { GOAL_DRAFT_RECORD_PREFIX, __resetForTests, loadGoalDraft, saveGoalDraft } from '../utils/goalDrafts'
import { SLOT, makeLoop, renderPopover } from './autoNudgePopoverFixtures'

const testNowMs = () => Date.now() // flake-ok: relative stamps are compared synchronously within each test

const CONFLICT = 'A newer goal was saved on another device. Use it, or replace it with your text.'
const CLEARED = 'This goal was cleared on another device. Keep it cleared, or replace it with your text.'

type StoredRow = { draft: Record<string, unknown> | null, updated_at: number }

/**
 * A browser-only copy (written before drafts synced) meets a goal, or a Clear,
 * the store already confirmed. The mock is the store: a migration fills only an
 * empty slot and otherwise answers 409 with the record, whatever its browser
 * edit time, and a live write lands only on the current record's stamp.
 */
function stubConfirmedStore(confirmed: StoredRow['draft']) {
  const openedAt = testNowMs()
  // The browser copy carries a later edit time than the confirmed record, as a
  // skewed browser clock inside the store's five-minute clamp would give it.
  saveGoalDraft(SLOT, { message: 'browser-only copy', idleSecs: 60, maxCycles: 0 }, openedAt + 60_000)
  const store: { row: StoredRow } = { row: { draft: confirmed, updated_at: openedAt - 1_000 } }
  const puts: Record<string, unknown>[] = []
  const starts: Record<string, unknown>[] = []
  const respond = (status: number, body: unknown) => ({ ok: status < 300, status, json: () => Promise.resolve(body) })
  const conflict = () => respond(409, {
    code: 'goal_draft_conflict',
    error: 'goal draft changed since it was loaded',
    current: store.row,
  })
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (String(url).startsWith('/api/crons')) return Promise.resolve(respond(200, { jobs: [] }))
    if (String(url) === '/api/autonudge' && init?.method === 'POST') {
      starts.push(JSON.parse(String(init.body)) as Record<string, unknown>)
      return Promise.resolve(respond(200, { loop: makeLoop() }))
    }
    if (init?.method !== 'PUT') return Promise.resolve(respond(200, store.row))
    const sent = JSON.parse(String(init.body)) as Record<string, unknown>
    puts.push(sent)
    if (sent.migration === true) {
      if (store.row.updated_at > 0) return Promise.resolve(conflict())
      store.row = { draft: sent.draft as StoredRow['draft'], updated_at: Number(sent.updated_at) }
      return Promise.resolve(respond(200, store.row))
    }
    if (sent.base_updated_at !== store.row.updated_at) return Promise.resolve(conflict())
    store.row = { draft: sent.draft as StoredRow['draft'], updated_at: store.row.updated_at + 1 }
    return Promise.resolve(respond(200, store.row))
  }) as unknown as typeof fetch)
  return { store, puts, starts }
}

describe('AutoNudgePopover goal-draft conflict actions', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const goalBox = () => screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i) as HTMLTextAreaElement
  const start = () => screen.getByRole('button', { name: /Start loop/i })
  const confirmedGoal = { message: 'goal confirmed on another device', idle_secs: 90, max_cycles: 4 }

  it('a browser-only copy over a confirmed goal is a conflict with two actions, never a silent upload or Retry', async () => {
    const { store, puts } = stubConfirmedStore(confirmedGoal)
    renderPopover(null)

    expect(await screen.findByTestId('goal-draft-sync-error')).toHaveTextContent(CONFLICT)
    expect(puts).toHaveLength(1)
    expect(puts[0]).toMatchObject({ migration: true, draft: { message: 'browser-only copy' } })
    expect(store.row.draft).toEqual(confirmedGoal)
    expect(goalBox().value).toBe('browser-only copy')
    expect(screen.getByRole('button', { name: 'Use newer goal' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Replace with my text' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: /^Retry$/i })).toBeNull()
    expect(start()).toBeDisabled()
  })

  it('a reopen keeps the refused browser copy as the conflict instead of uploading it', async () => {
    const { store, puts } = stubConfirmedStore(confirmedGoal)
    const first = renderPopover(null)
    await screen.findByTestId('goal-draft-sync-error')
    first.unmount()

    renderPopover(null)

    expect(await screen.findByTestId('goal-draft-sync-error')).toHaveTextContent(CONFLICT)
    expect(goalBox().value).toBe('browser-only copy')
    expect(puts).toHaveLength(1)
    expect(store.row.draft).toEqual(confirmedGoal)
    expect(start()).toBeDisabled()
  })

  it('Use newer goal shows the confirmed goal, drops the pending copy without a write, and Start runs it', async () => {
    const { store, puts, starts } = stubConfirmedStore(confirmedGoal)
    renderPopover(null)
    await screen.findByTestId('goal-draft-sync-error')

    fireEvent.click(screen.getByRole('button', { name: 'Use newer goal' }))

    await waitFor(() => expect(goalBox().value).toBe(confirmedGoal.message))
    expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    expect(puts).toHaveLength(1)
    expect(store.row.draft).toEqual(confirmedGoal)
    expect(loadGoalDraft(SLOT)).toEqual({ message: confirmedGoal.message, idleSecs: 90, maxCycles: 4 })
    await waitFor(() => expect(start()).toBeEnabled())
    await act(async () => { fireEvent.click(start()) })
    await waitFor(() => expect(starts).toHaveLength(1))
    expect(starts[0]).toMatchObject({ slot_key: SLOT, message: confirmedGoal.message })
  })

  it("Use newer goal keeps another tab's draft saved during its read as a conflict", async () => {
    const { store } = stubConfirmedStore(confirmedGoal)
    renderPopover(null)
    await screen.findByTestId('goal-draft-sync-error')
    // Hold the next goal-draft read: the one "Use newer goal" sends.
    const serve = globalThis.fetch
    let release = () => {}
    const held = new Promise<void>(resolve => { release = resolve })
    let heldReads = 0
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons') || init?.method) return serve(url, init)
      heldReads += 1
      return held.then(() => serve(url, init))
    }) as unknown as typeof fetch)

    fireEvent.click(screen.getByRole('button', { name: 'Use newer goal' }))
    await waitFor(() => expect(heldReads).toBe(1))
    // Tab B edits the slot and closes, flushing its pending draft to shared storage.
    const tabBKey = GOAL_DRAFT_RECORD_PREFIX + encodeURIComponent(SLOT)
    const tabBRecord = JSON.stringify({
      updatedAt: testNowMs() + 120_000,
      value: { message: 'tab B unsaved edit', idleSecs: 60, maxCycles: 0 },
      baseUpdatedAt: store.row.updated_at,
    })
    localStorage.setItem(tabBKey, tabBRecord)
    await act(async () => { release(); await held })

    expect(await screen.findByTestId('goal-draft-sync-error')).toHaveTextContent(CONFLICT)
    expect(localStorage.getItem(tabBKey)).toBe(tabBRecord)
    expect(loadGoalDraft(SLOT)).toMatchObject({ message: 'tab B unsaved edit' })
    expect(store.row.draft).toEqual(confirmedGoal)
    expect(start()).toBeDisabled()
  })

  it('Replace with my text is the explicit compare-and-set on the confirmed goal', async () => {
    const { store, puts } = stubConfirmedStore(confirmedGoal)
    renderPopover(null)
    await screen.findByTestId('goal-draft-sync-error')
    const confirmedAt = store.row.updated_at

    fireEvent.click(screen.getByRole('button', { name: 'Replace with my text' }))

    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull())
    expect(puts.at(-1)).toMatchObject({
      migration: false,
      base_updated_at: confirmedAt,
      draft: { message: 'browser-only copy' },
    })
    expect(store.row.draft).toMatchObject({ message: 'browser-only copy' })
    expect(goalBox().value).toBe('browser-only copy')
    await waitFor(() => expect(start()).toBeEnabled())
  })

  it('a Clear confirmed on another device offers the Keep it cleared its notice names, and it keeps the goal cleared', async () => {
    const { store, puts } = stubConfirmedStore(null)
    renderPopover(null)

    expect(await screen.findByTestId('goal-draft-sync-error')).toHaveTextContent(CLEARED)
    expect(store.row.draft).toBeNull()
    // The notice says "Keep it cleared", so a button must say it too: there is
    // no newer goal to use, and "Use newer goal" would name a choice not offered.
    expect(screen.getByRole('button', { name: 'Keep it cleared' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Replace with my text' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Use newer goal' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Keep it cleared' }))

    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull())
    expect(goalBox().value).not.toBe('browser-only copy')
    expect(loadGoalDraft(SLOT)).toBeNull()
    expect(puts).toHaveLength(1)
    expect(store.row.draft).toBeNull()
  })
})
