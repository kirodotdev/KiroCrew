import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { __resetForTests, loadGoalDraft, saveGoalDraft } from '../utils/goalDrafts'
import { SLOT, makeLoop, renderPopover } from './autoNudgePopoverFixtures'

const testNowMs = () => Date.now() // flake-ok: relative stamps are compared synchronously within each test

/** The conflict notice: another device saved a newer goal; "Replace with my text" replaces it. */
const CONFLICT = 'A newer goal was saved on another device. Use it, or replace it with your text.'

type StoredRow = { draft: Record<string, unknown> | null, updated_at: number }

/**
 * The open-time migration: this browser holds a goal the store's empty slot
 * lacks, so opening uploads it, and the user types while that upload is in
 * flight. The mock is the store's compare-and-set: a live write lands only on
 * the current record's stamp, and a migration fills only an empty slot, so
 * over another device's record it answers 409 with that record.
 */
function stubMigratingStore(options: { otherDeviceSaves: boolean }) {
  const openedAt = testNowMs()
  const goalA = { message: 'browser goal A', idle_secs: 60, max_cycles: 0 }
  const goalB = { message: 'goal B saved on another device', idle_secs: 90, max_cycles: 4 }
  saveGoalDraft(SLOT, { message: goalA.message, idleSecs: 60, maxCycles: 0 }, openedAt)
  const store: { row: StoredRow } = { row: { draft: null, updated_at: 0 } }
  const puts: Record<string, unknown>[] = []
  const starts: Record<string, unknown>[] = []
  const migration: { answer?: () => void } = {}
  const respond = (status: number, body: unknown) => ({ ok: status < 300, status, json: () => Promise.resolve(body) })
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
      return new Promise(resolve => {
        migration.answer = () => {
          // Another device saves goal B before this upload lands, so the store
          // keeps B and refuses the upload as a conflict.
          if (options.otherDeviceSaves) {
            store.row = { draft: goalB, updated_at: openedAt + 1_000 }
            resolve(respond(409, {
              code: 'goal_draft_conflict',
              error: 'goal draft changed since it was loaded',
              current: store.row,
            }))
            return
          }
          store.row = { draft: sent.draft as Record<string, unknown>, updated_at: Number(sent.updated_at) }
          resolve(respond(200, store.row))
        }
      })
    }
    if (sent.base_updated_at !== store.row.updated_at) {
      return Promise.resolve(respond(409, {
        code: 'goal_draft_conflict',
        error: 'goal draft changed since it was loaded',
        current: store.row,
      }))
    }
    store.row = { draft: sent.draft as Record<string, unknown> | null, updated_at: store.row.updated_at + 1 }
    return Promise.resolve(respond(200, store.row))
  }) as unknown as typeof fetch)
  return { goalB, store, puts, starts, migration }
}

describe('AutoNudgePopover goal text typed during the open-time migration', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const goalBox = () => screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i) as HTMLTextAreaElement
  const start = () => screen.getByRole('button', { name: /Start loop/i })
  const typed = 'goal text typed during the migration'

  it('meets another device\'s goal the refused migration returns as a conflict, and only Replace with my text replaces it', async () => {
    const { goalB, store, puts, starts, migration } = stubMigratingStore({ otherDeviceSaves: true })
    renderPopover(null)
    await waitFor(() => expect(puts).toHaveLength(1))
    expect(puts[0]).toMatchObject({ migration: true })
    fireEvent.change(goalBox(), { target: { value: typed } })
    expect(start()).toBeDisabled()

    await act(async () => { migration.answer?.() })

    // The typed text stays, as an edit on the goal the editor showed: goal B
    // meets it as a conflict, and its own save is refused rather than landing on B.
    expect(await screen.findByTestId('goal-draft-sync-error')).toHaveTextContent(CONFLICT)
    expect(goalBox().value).toBe(typed)
    await waitFor(() => expect(puts).toHaveLength(2))
    expect(puts[1]).toMatchObject({ migration: false, draft: { message: typed } })
    expect(puts[1].base_updated_at).not.toBe(store.row.updated_at)
    expect(store.row.draft).toEqual(goalB)
    expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(CONFLICT)
    expect(start()).toBeDisabled()
    await act(async () => { fireEvent.click(start()) })
    expect(starts).toHaveLength(0)

    // Replace with my text puts the typed text over goal B; only then Start runs it.
    const replacedAt = store.row.updated_at
    fireEvent.click(screen.getByRole('button', { name: 'Replace with my text' }))
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull())
    expect(puts.at(-1)).toMatchObject({ base_updated_at: replacedAt, draft: { message: typed } })
    expect(store.row.draft).toMatchObject({ message: typed })
    expect(goalBox().value).toBe(typed)
    await waitFor(() => expect(start()).toBeEnabled())
    await act(async () => { fireEvent.click(start()) })
    await waitFor(() => expect(starts).toHaveLength(1))
    expect(starts[0]).toMatchObject({ slot_key: SLOT, message: typed })
  })

  it('saves the typed text normally when the migration is confirmed', async () => {
    const { store, puts, starts, migration } = stubMigratingStore({ otherDeviceSaves: false })
    renderPopover(null)
    await waitFor(() => expect(puts).toHaveLength(1))
    fireEvent.change(goalBox(), { target: { value: typed } })

    await act(async () => { migration.answer?.() })
    const confirmedAt = store.row.updated_at

    // The answer is this browser's upload, so the typed text saves on it.
    await waitFor(() => expect(puts).toHaveLength(2))
    expect(puts[1]).toMatchObject({ migration: false, base_updated_at: confirmedAt, draft: { message: typed } })
    expect(store.row.draft).toMatchObject({ message: typed })
    await waitFor(() => expect(start()).toBeEnabled())
    expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    expect(goalBox().value).toBe(typed)
    expect(loadGoalDraft(SLOT)?.message).toBe(typed)
    await act(async () => { fireEvent.click(start()) })
    await waitFor(() => expect(starts).toHaveLength(1))
    expect(starts[0]).toMatchObject({ slot_key: SLOT, message: typed })
    expect(puts).toHaveLength(2)
  })
})

describe('AutoNudgePopover reopened while this tab\'s own write is in flight', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const goalBox = () => screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i) as HTMLTextAreaElement

  it('reopening while the close flush still saves reads the copy that save confirms, not a conflict', async () => {
    const now = testNowMs()
    let server: { draft: Record<string, unknown> | null; updated_at: number } = {
      draft: { message: 'old goal', idle_secs: 60, max_cycles: 0 },
      updated_at: now,
    }
    const puts: Record<string, unknown>[] = []
    let releaseFlush!: () => void
    // The store's compare-and-set: a write on a stale base meets a 409.
    const answer = (sent: Record<string, unknown>) => {
      if (Number(sent.base_updated_at ?? 0) !== server.updated_at) {
        return {
          ok: false,
          status: 409,
          json: () => Promise.resolve({ code: 'goal_draft_conflict', current: server }),
        }
      }
      server = { draft: sent.draft as Record<string, unknown>, updated_at: Number(sent.updated_at) }
      return { ok: true, json: () => Promise.resolve(server) }
    }
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        if (puts.length === 1) {
          return new Promise(resolve => { releaseFlush = () => resolve(answer(sent)) })
        }
        return Promise.resolve(answer(sent))
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(server) })
    }) as unknown as typeof fetch)
    // This browser already holds the confirmed copy, so the first open is idle.
    saveGoalDraft(SLOT, { message: 'old goal', idleSecs: 60, maxCycles: 0 }, now)

    const first = renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    fireEvent.change(goalBox(), { target: { value: 'saved on close' } })
    // Closing before the debounce fires sends the edit as the close flush.
    first.unmount()
    await waitFor(() => expect(puts).toHaveLength(1))

    renderPopover(null)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    await act(async () => { releaseFlush() })

    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    expect(goalBox().value).toBe('saved on close')
    expect(screen.queryByTestId('goal-draft-sync-error')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Start loop/i })).toBeEnabled()
    // The reopen added no write of its own: the close flush was the only save.
    expect(puts).toHaveLength(1)
  })
})
