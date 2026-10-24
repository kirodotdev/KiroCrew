import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'
import {
  __resetForTests,
  loadGoalDraft,
  loadGoalDraftSnapshot,
  pendingGoalDraftClear,
  saveGoalDraft,
} from '../utils/goalDrafts'
import {
  isGoalRecordKey,
  makeLoop,
  renderBridgedPopover,
  renderPopover,
  renderStatefulPopover,
} from './autoNudgePopoverFixtures'

const testNowMs = () => Date.now() // flake-ok: relative stamps are compared synchronously within each test

describe('AutoNudgePopover goal clear tombstones', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    // The popover fetches on OPEN (reads /api/crons to list this slot's
    // watches) and on Save/Stop. Stub so nothing escapes the test.
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const goalBox = () => screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i) as HTMLTextAreaElement

  it('keeps a local clear-write failure visible instead of closing as if both stores cleared', async () => {
    const slot = 'chat-clear-local-failure'
    const oldStamp = testNowMs() - 5_000
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 }, oldStamp)
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      const sent = JSON.parse(String(init?.body)) as Record<string, unknown>
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: sent.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))

    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(k: string, v: string) {
      if (isGoalRecordKey(k)) throw new Error('QuotaExceeded')
      return orig.call(this, k, v)
    }
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
      await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
      expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
      expect(goalBox().value).toContain('north star')
      // The editor shows the cleared template, so the failure must describe the
      // CLEAR that did not finish, not a retained draft that no longer exists.
      expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(
        "Couldn't clear this goal on every device yet",
      )
      expect(screen.getByTestId('goal-draft-sync-error')).not.toHaveTextContent('copy is still here')
    } finally {
      Storage.prototype.setItem = orig
    }

    expect(loadGoalDraftSnapshot(slot)).toEqual({
      draft: { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 },
      updatedAt: oldStamp,
    })
  })

  it('keeps a remote clear-write failure visible with a durable local tombstone', async () => {
    const slot = 'chat-clear-remote-failure'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      return Promise.resolve({
        ok: false,
        status: 503,
        json: () => Promise.resolve({ error: 'temporarily unavailable' }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))

    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(goalBox().value).toContain('north star')
    expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(
      "Couldn't clear this goal on every device yet. Retry to finish clearing it.",
    )
    expect(screen.getByTestId('goal-draft-sync-error')).toHaveAttribute('role', 'alert')
    expect(screen.getByRole('button', { name: /^Retry$/i })).toBeInTheDocument()
    // The unfinished clear is what keeps Start disabled, so it is Start's description.
    const start = screen.getByRole('button', { name: /Start loop/i })
    expect(start).toBeDisabled()
    expect(screen.getByTestId('goal-draft-sync-error')).toHaveAttribute('id')
    expect(start).toHaveAttribute('aria-describedby', screen.getByTestId('goal-draft-sync-error').getAttribute('id'))
    expect(start).toHaveAccessibleDescription(
      "Couldn't clear this goal on every device yet. Retry to finish clearing it.",
    )
    expect(loadGoalDraftSnapshot(slot).draft).toBeNull()
    expect(loadGoalDraftSnapshot(slot).updatedAt).toBeGreaterThan(0)
  })

  it('closes only after the clear tombstone is durable locally and remotely', async () => {
    const slot = 'chat-clear-success'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    let clearPut: Record<string, unknown> | null = null
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      clearPut = JSON.parse(String(init?.body)) as Record<string, unknown>
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: clearPut.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))

    await waitFor(() => expect(screen.getByTestId('popover-open')).toHaveTextContent('false'))
    expect(clearPut).toMatchObject({ draft: null, migration: false })
    expect(loadGoalDraftSnapshot(slot).draft).toBeNull()
    expect(loadGoalDraftSnapshot(slot).updatedAt).toBeGreaterThan(0)
  })

  it('clears from a second device that opened on the live loop and never read the shared draft', async () => {
    const slot = 'chat-clear-second-device'
    // Another device's Start uploaded this goal. This device holds no local record
    // and opened the popover on the loop, so it never read the canonical draft.
    let stored: { draft: Record<string, unknown> | null, updated_at: number } = {
      draft: { message: 'active loop goal', idle_secs: 90, max_cycles: 3 },
      updated_at: testNowMs() - 5_000,
    }
    const puts: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      if (!String(url).startsWith('/api/autonudge/draft/')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })
      }
      if (init?.method !== 'PUT') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(stored) })
      }
      const sent = JSON.parse(String(init.body)) as Record<string, unknown>
      puts.push(sent)
      if (sent.base_updated_at !== stored.updated_at) {
        return Promise.resolve({
          ok: false,
          status: 409,
          json: () => Promise.resolve({
            code: 'goal_draft_conflict',
            error: 'goal draft changed since it was loaded',
            current: stored,
          }),
        })
      }
      stored = { draft: sent.draft as Record<string, unknown> | null, updated_at: stored.updated_at + 1 }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(stored) })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))

    await waitFor(() => expect(screen.getByTestId('popover-open')).toHaveTextContent('false'))
    expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    expect(puts).toHaveLength(1)
    expect(puts[0]).toMatchObject({ draft: null, migration: false })
    expect(stored.draft).toBeNull()
    expect(loadGoalDraftSnapshot(slot).draft).toBeNull()
  })

  it('keeps and queues an edit typed while loop deletion is in flight', async () => {
    const slot = 'chat-clear-delete-edit'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    const puts: Record<string, unknown>[] = []
    let resolveDelete!: () => void
    let deleteStarted = false
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        deleteStarted = true
        return new Promise(resolve => {
          resolveDelete = () => resolve({ ok: true, json: () => Promise.resolve({}) })
        })
      }
      if (!init?.method) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: null, updated_at: 0 }),
        })
      }
      const sent = JSON.parse(String(init.body)) as Record<string, unknown>
      puts.push(sent)
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(deleteStarted).toBe(true))

    fireEvent.change(goalBox(), { target: { value: 'edit while deleting loop' } })
    resolveDelete()

    await waitFor(() => expect(puts).toHaveLength(2))
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(goalBox().value).toBe('edit while deleting loop')
    expect(puts[0]).toMatchObject({ draft: null, migration: false })
    expect((puts[1].draft as Record<string, unknown>).message).toBe('edit while deleting loop')
    expect(loadGoalDraft(slot)?.message).toBe('edit while deleting loop')
  })

  it.each([
    {
      name: 'idle seconds',
      slot: 'chat-clear-delete-idle-edit',
      label: 'Seconds between nudges',
      value: '321',
      property: 'idle_secs',
      expected: 321,
    },
    {
      name: 'max cycles',
      slot: 'chat-clear-delete-cycle-edit',
      label: 'Max cycles (0 = infinite)',
      value: '7',
      property: 'max_cycles',
      expected: 7,
    },
  ])('keeps a $name edit typed while loop deletion is in flight', async ({
    slot, label, value, property, expected,
  }) => {
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 90, maxCycles: 3 })
    const puts: Record<string, unknown>[] = []
    let resolveDelete!: () => void
    let deleteStarted = false
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        deleteStarted = true
        return new Promise(resolve => {
          resolveDelete = () => resolve({ ok: true, json: () => Promise.resolve({}) })
        })
      }
      const sent = JSON.parse(String(init?.body)) as Record<string, unknown>
      puts.push(sent)
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({
      slot_key: slot,
      active: false,
      message: 'stale durable goal',
      idle_secs: 90,
      max_cycles: 3,
    }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(deleteStarted).toBe(true))

    fireEvent.change(screen.getByLabelText(label), { target: { value } })
    resolveDelete()

    await waitFor(() => expect(puts).toHaveLength(2))
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(screen.getByLabelText(label)).toHaveValue(Number(value))
    expect((puts[1].draft as Record<string, unknown>)[property]).toBe(expected)
  })

  it('keeps a newer edit visible through remote clear success and remount', async () => {
    const slot = 'chat-clear-newer-edit'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    const puts: Record<string, unknown>[] = []
    let canonicalDraft: Record<string, unknown> | null = null
    let canonicalAt = 0
    let resolveClear!: () => void
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      if (!init?.method) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: canonicalDraft, updated_at: canonicalAt }),
        })
      }
      const sent = JSON.parse(String(init.body)) as Record<string, unknown>
      puts.push(sent)
      if (puts.length === 1) {
        return new Promise(resolve => {
          resolveClear = () => resolve({
            ok: true,
            json: () => Promise.resolve({ draft: null, updated_at: sent.updated_at }),
          })
        })
      }
      canonicalDraft = sent.draft as Record<string, unknown>
      canonicalAt = Number(sent.updated_at)
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: canonicalDraft, updated_at: canonicalAt }),
      })
    }) as unknown as typeof fetch)

    const first = renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(puts).toHaveLength(1))

    fireEvent.change(goalBox(), { target: { value: 'newer edit during clear' } })
    resolveClear()

    await waitFor(() => expect(puts).toHaveLength(2))
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(goalBox().value).toBe('newer edit during clear')
    expect(puts[0]).toMatchObject({ draft: null, migration: false })
    expect((puts[1].draft as Record<string, unknown>).message).toBe('newer edit during clear')
    expect(loadGoalDraft(slot)?.message).toBe('newer edit during clear')

    first.unmount()
    renderPopover(null, slot)
    expect(goalBox().value).toBe('newer edit during clear')
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    expect(goalBox().value).toBe('newer edit during clear')
  })

  it('ignores a failed clear completion after a newer edit is already queued', async () => {
    const slot = 'chat-clear-failure-newer-edit'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    const puts: Record<string, unknown>[] = []
    let resolveClear!: () => void
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      const sent = JSON.parse(String(init?.body)) as Record<string, unknown>
      puts.push(sent)
      if (puts.length === 1) {
        return new Promise(resolve => {
          resolveClear = () => resolve({
            ok: false,
            status: 503,
            json: () => Promise.resolve({ error: 'temporarily unavailable' }),
          })
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(puts).toHaveLength(1))

    fireEvent.change(goalBox(), { target: { value: 'edit after local clear' } })
    resolveClear()

    await waitFor(() => expect(puts).toHaveLength(2))
    expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(goalBox().value).toBe('edit after local clear')
    expect((puts[1].draft as Record<string, unknown>).message).toBe('edit after local clear')
    expect(loadGoalDraft(slot)?.message).toBe('edit after local clear')
  })

  it('retries the newer edit instead of the failed clear tombstone', async () => {
    const slot = 'chat-clear-failure-retry-edit'
    const oldStamp = testNowMs() - 5_000
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 }, oldStamp)
    const puts: Record<string, unknown>[] = []
    let remoteHealthy = false
    let resolveClear!: () => void
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      if (!init?.method) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            draft: { message: 'stale durable goal', idle_secs: 60, max_cycles: 0 },
            updated_at: oldStamp,
          }),
        })
      }
      const sent = JSON.parse(String(init.body)) as Record<string, unknown>
      puts.push(sent)
      if (puts.length === 1) {
        return new Promise(resolve => {
          resolveClear = () => resolve({
            ok: false,
            status: 503,
            json: () => Promise.resolve({ error: 'temporarily unavailable' }),
          })
        })
      }
      if (!remoteHealthy) {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ error: 'temporarily unavailable' }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(puts).toHaveLength(1))

    fireEvent.change(goalBox(), { target: { value: 'retry this newer edit' } })
    resolveClear()

    await waitFor(() => expect(puts).toHaveLength(2))
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    expect(goalBox().value).toBe('retry this newer edit')
    expect(loadGoalDraft(slot)?.message).toBe('retry this newer edit')

    remoteHealthy = true
    fireEvent.click(screen.getByRole('button', { name: /^Retry$/i }))

    await waitFor(() => expect(puts).toHaveLength(3))
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull())
    expect((puts[2].draft as Record<string, unknown>).message).toBe('retry this newer edit')
    expect(goalBox().value).toBe('retry this newer edit')
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
  })

  it('preserves a failed clear tombstone and visible failure across remount', async () => {
    const slot = 'chat-clear-remount'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      return Promise.resolve({
        ok: false,
        status: 503,
        json: () => Promise.resolve({ error: 'temporarily unavailable' }),
      })
    }) as unknown as typeof fetch)

    const first = renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    first.unmount()

    renderPopover(null, slot)
    expect(goalBox().value).toContain('north star')
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    // The remount reconciles the durable tombstone, so its failure is still a
    // clear that has not finished, not a draft this device is holding.
    expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(
      "Couldn't clear this goal on every device yet",
    )
    expect(loadGoalDraftSnapshot(slot).draft).toBeNull()
  })

  it('keeps Retry failed when a local clear failure is followed by a remote failure', async () => {
    const slot = 'chat-clear-local-then-remote'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    let retrying = false
    let retryReads = 0
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      if (retrying && !init?.method) {
        retryReads += 1
        return Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ error: 'temporarily unavailable' }),
        })
      }
      const sent = JSON.parse(String(init?.body)) as Record<string, unknown>
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: sent.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(k: string, v: string) {
      if (isGoalRecordKey(k)) throw new Error('QuotaExceeded')
      return orig.call(this, k, v)
    }
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
      await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    } finally {
      Storage.prototype.setItem = orig
    }

    retrying = true
    fireEvent.click(screen.getByRole('button', { name: /^Retry$/i }))
    await waitFor(() => expect(retryReads).toBe(1))
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(goalBox().value).toContain('north star')
    // Retry re-runs the open reconciliation over the retained tombstone; a
    // second failure must keep describing the clear, not fall back to draft copy.
    expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(
      "Couldn't clear this goal on every device yet",
    )
  })

  describe('through the dashboard bridge, which remounts the editor on the cleared loop', () => {
    type Stored = { draft: Record<string, unknown> | null, updated_at: number }
    const baseStamp = () => testNowMs() - 5_000

    /** The draft route as the gateway answers it: compare-and-set on the base. */
    function stubGateway(stored: { current: Stored }, opts: {
      holdDelete?: boolean
      holdFirstPut?: boolean
      putHealthy?: () => boolean
    } = {}) {
      const puts: Record<string, unknown>[] = []
      const control = {
        deleteStarted: false,
        resolveDelete: () => {},
        failDelete: () => {},
        resolveFirstPut: () => {},
      }
      const answerPut = (sent: Record<string, unknown>) => {
        if (opts.putHealthy && !opts.putHealthy()) {
          return { ok: false, status: 503, json: () => Promise.resolve({ error: 'temporarily unavailable' }) }
        }
        if (sent.base_updated_at !== stored.current.updated_at) {
          return {
            ok: false,
            status: 409,
            json: () => Promise.resolve({
              code: 'goal_draft_conflict',
              error: 'goal draft changed since it was loaded',
              current: stored.current,
            }),
          }
        }
        stored.current = {
          draft: sent.draft as Record<string, unknown> | null,
          updated_at: Math.max(Number(sent.updated_at), stored.current.updated_at + 1),
        }
        const answer = stored.current
        return { ok: true, json: () => Promise.resolve(answer) }
      }
      vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
        if (String(url).startsWith('/api/crons')) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
        }
        if (init?.method === 'DELETE') {
          control.deleteStarted = true
          const answer = { ok: true, json: () => Promise.resolve({}) }
          if (!opts.holdDelete) return Promise.resolve(answer)
          return new Promise(resolve => {
            control.resolveDelete = () => resolve(answer)
            control.failDelete = () => resolve({
              ok: false,
              status: 500,
              json: () => Promise.resolve({ error: 'loop removal failed' }),
            })
          })
        }
        if (!String(url).startsWith('/api/autonudge/draft/')) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })
        }
        if (init?.method !== 'PUT') {
          const answer = stored.current
          return Promise.resolve({ ok: true, json: () => Promise.resolve(answer) })
        }
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        if (opts.holdFirstPut && puts.length === 1) {
          return new Promise(resolve => { control.resolveFirstPut = () => resolve(answerPut(sent)) })
        }
        return Promise.resolve(answerPut(sent))
      }) as unknown as typeof fetch)
      return { puts, control }
    }

    function pressClear() {
      fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
      fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    }

    it('keeps text typed while the loop removal is pending and saves it on the tombstone', async () => {
      const slot = 'chat-bridge-clear-delete-edit'
      const stored = { current: { draft: { message: 'active loop goal', idle_secs: 90, max_cycles: 3 }, updated_at: baseStamp() } as Stored }
      const { puts, control } = stubGateway(stored, { holdDelete: true })

      renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
      pressClear()
      await waitFor(() => expect(control.deleteStarted).toBe(true))
      fireEvent.change(goalBox(), { target: { value: 'typed while the loop is removed' } })
      control.resolveDelete()

      await waitFor(() => expect(puts).toHaveLength(2))
      await waitFor(() => expect(stored.current.draft?.message).toBe('typed while the loop is removed'))
      // The editor did remount: the cleared loop's controls are gone.
      expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull()
      expect(puts[0]).toMatchObject({ draft: null, migration: false })
      const tombstoneStamp = Math.max(Number(puts[0].updated_at), Number(puts[0].base_updated_at) + 1)
      expect(puts[1]).toMatchObject({ base_updated_at: tombstoneStamp, migration: false })
      expect(goalBox().value).toBe('typed while the loop is removed')
      expect(loadGoalDraft(slot)?.message).toBe('typed while the loop is removed')
      expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
      await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
      expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    })

    it('keeps text typed while the tombstone write is pending and saves it on the tombstone', async () => {
      const slot = 'chat-bridge-clear-put-edit'
      const stored = { current: { draft: { message: 'active loop goal', idle_secs: 90, max_cycles: 3 }, updated_at: baseStamp() } as Stored }
      const { puts, control } = stubGateway(stored, { holdFirstPut: true })

      renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
      pressClear()
      await waitFor(() => expect(puts).toHaveLength(1))
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull())
      fireEvent.change(goalBox(), { target: { value: 'typed while the tombstone saves' } })
      control.resolveFirstPut()

      await waitFor(() => expect(stored.current.draft?.message).toBe('typed while the tombstone saves'))
      // One tombstone, then the typed draft on its stamp: the remounted editor
      // does not resend the clear on the stale base the server then refuses.
      expect(puts).toHaveLength(2)
      expect(puts[0]).toMatchObject({ draft: null, migration: false })
      const tombstoneStamp = Math.max(Number(puts[0].updated_at), Number(puts[0].base_updated_at) + 1)
      expect(puts[1]).toMatchObject({ base_updated_at: tombstoneStamp, migration: false })
      expect((puts[1].draft as Record<string, unknown>).message).toBe('typed while the tombstone saves')
      expect(goalBox().value).toBe('typed while the tombstone saves')
      expect(loadGoalDraft(slot)?.message).toBe('typed while the tombstone saves')
      expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
      await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
      expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    })

    it('with nothing typed ends in the tombstone, closes, and reopens on the default template', async () => {
      const slot = 'chat-bridge-clear-untouched'
      const stored = { current: { draft: { message: 'active loop goal', idle_secs: 90, max_cycles: 3 }, updated_at: baseStamp() } as Stored }
      const { puts, control } = stubGateway(stored, { holdFirstPut: true })

      renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
      pressClear()
      // The tombstone answers only after the hand-up has remounted the editor.
      await waitFor(() => expect(puts).toHaveLength(1))
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull())
      control.resolveFirstPut()

      await waitFor(() => expect(screen.getByTestId('popover-open')).toHaveTextContent('false'))
      expect(puts).toHaveLength(1)
      expect(puts[0]).toMatchObject({ draft: null, migration: false })
      expect(stored.current.draft).toBeNull()
      expect(loadGoalDraftSnapshot(slot).draft).toBeNull()
      expect(loadGoalDraftSnapshot(slot).updatedAt).toBe(stored.current.updated_at)

      fireEvent.click(screen.getByTestId('reopen-popover'))
      expect(goalBox().value).toContain('north star')
      await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
      expect(goalBox().value).toContain('north star')
      expect(puts).toHaveLength(1)
    })

    it('shows a failed clear on the remounted editor and finishes it on Retry', async () => {
      const slot = 'chat-bridge-clear-failure'
      const stored = { current: { draft: { message: 'active loop goal', idle_secs: 90, max_cycles: 3 }, updated_at: baseStamp() } as Stored }
      let healthy = false
      const { puts, control } = stubGateway(stored, { holdFirstPut: true, putHealthy: () => healthy })

      renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
      pressClear()
      await waitFor(() => expect(puts).toHaveLength(1))
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull())
      control.resolveFirstPut()

      await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(
        "Couldn't clear this goal on every device yet. Retry to finish clearing it.",
      ))
      expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull()
      expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
      expect(goalBox().value).toContain('north star')
      expect(screen.getByRole('button', { name: /Start loop/i })).toBeDisabled()
      expect(loadGoalDraftSnapshot(slot).draft).toBeNull()

      healthy = true
      fireEvent.click(screen.getByRole('button', { name: /^Retry$/i }))
      await waitFor(() => expect(screen.getByTestId('popover-open')).toHaveTextContent('false'))
      expect(stored.current.draft).toBeNull()
      expect(puts.every(put => put.draft === null)).toBe(true)
      expect(loadGoalDraftSnapshot(slot).draft).toBeNull()
    })

    describe('when the removed frame remounts the editor before the DELETE answers', () => {
      /** This device saved the goal it started, so it holds the canonical copy. */
      function savedGoal(slot: string) {
        const stored = { current: { draft: { message: 'active loop goal', idle_secs: 90, max_cycles: 3 }, updated_at: baseStamp() } as Stored }
        saveGoalDraft(slot, { message: 'active loop goal', idleSecs: 90, maxCycles: 3 }, stored.current.updated_at)
        return stored
      }

      /** The server sends `removed` inside the remove call, before the DELETE
       *  answers. The store drops the automation, which re-keys the editor. */
      async function removedFrameBeforeDeleteAnswers(control: { deleteStarted: boolean }) {
        await waitFor(() => expect(control.deleteStarted).toBe(true))
        fireEvent.click(screen.getByTestId('remove-automation'))
        await waitFor(() => expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull())
        // Let an editor that missed the Clear read and show the shared copy.
        await act(async () => {
          await new Promise(resolve => setTimeout(resolve, 0))
          await Promise.resolve()
        })
      }

      it('waits for the Clear, never offers the cleared goal to Start, then closes on the template', async () => {
        const slot = 'chat-bridge-clear-removed-first'
        const stored = savedGoal(slot)
        const { puts, control } = stubGateway(stored, { holdDelete: true })

        renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
        pressClear()
        await removedFrameBeforeDeleteAnswers(control)
        expect(screen.getByTestId('goal-draft-sync-status')).toHaveTextContent('Checking saved goal')
        expect(screen.getByRole('button', { name: /Start loop/i })).toBeDisabled()
        control.resolveDelete()

        await waitFor(() => expect(screen.getByTestId('popover-open')).toHaveTextContent('false'))
        expect(puts).toHaveLength(1)
        expect(puts[0]).toMatchObject({ draft: null, migration: false })
        expect(stored.current.draft).toBeNull()
        expect(loadGoalDraftSnapshot(slot).draft).toBeNull()
        expect(pendingGoalDraftClear(slot)).toBeUndefined()

        fireEvent.click(screen.getByTestId('reopen-popover'))
        expect(goalBox().value).toContain('north star')
        await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
        expect(goalBox().value).toContain('north star')
        expect(puts).toHaveLength(1)
      })

      it('shows the failed clear with Retry when the DELETE then fails', async () => {
        const slot = 'chat-bridge-clear-removed-delete-fails'
        const stored = savedGoal(slot)
        const { puts, control } = stubGateway(stored, { holdDelete: true })

        renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
        pressClear()
        await removedFrameBeforeDeleteAnswers(control)
        control.failDelete()

        await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(
          "Couldn't clear this goal on every device yet. Retry to finish clearing it.",
        ))
        expect(screen.getByRole('button', { name: /^Retry$/i })).toBeInTheDocument()
        expect(screen.getByRole('button', { name: /Start loop/i })).toBeDisabled()
        expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
        // The Clear ended with its removal: no registration is left, and no
        // tombstone was written, so the editor keeps the copy the store holds.
        expect(pendingGoalDraftClear(slot)).toBeUndefined()
        expect(puts).toHaveLength(0)
        expect(goalBox().value).toBe('active loop goal')
        expect(loadGoalDraft(slot)?.message).toBe('active loop goal')
      })

      it('keeps text typed in the new editor and saves it on the tombstone', async () => {
        const slot = 'chat-bridge-clear-removed-typed'
        const stored = savedGoal(slot)
        const { puts, control } = stubGateway(stored, { holdDelete: true })

        renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
        pressClear()
        await removedFrameBeforeDeleteAnswers(control)
        fireEvent.change(goalBox(), { target: { value: 'typed in the new editor' } })
        // An edit during the check keeps Start disabled until the Clear lands.
        expect(screen.getByRole('button', { name: /Start loop/i })).toBeDisabled()
        control.resolveDelete()

        await waitFor(() => expect(stored.current.draft?.message).toBe('typed in the new editor'))
        expect(puts).toHaveLength(2)
        expect(puts[0]).toMatchObject({ draft: null, migration: false })
        const tombstoneStamp = Math.max(Number(puts[0].updated_at), Number(puts[0].base_updated_at) + 1)
        expect(puts[1]).toMatchObject({ base_updated_at: tombstoneStamp, migration: false })
        expect(goalBox().value).toBe('typed in the new editor')
        expect(loadGoalDraft(slot)?.message).toBe('typed in the new editor')
        expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
        await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
        expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
      })
    })
  })

  it('closes after Retry confirms the clear even when local cache persistence fails', async () => {
    const slot = 'chat-clear-remote-then-local'
    const oldStamp = testNowMs() - 5_000
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 }, oldStamp)
    let retrying = false
    let retryPut: Record<string, unknown> | null = null
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      if (!retrying && init?.method === 'PUT') {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ error: 'temporarily unavailable' }),
        })
      }
      if (retrying && init?.method === 'PUT') {
        retryPut = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: null, updated_at: retryPut.updated_at }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'stale durable goal', idle_secs: 60, max_cycles: 0 },
          updated_at: oldStamp,
        }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())

    retrying = true
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(k: string, v: string) {
      if (isGoalRecordKey(k)) throw new Error('QuotaExceeded')
      return orig.call(this, k, v)
    }
    try {
      fireEvent.click(screen.getByRole('button', { name: /^Retry$/i }))
      await waitFor(() => expect(retryPut).not.toBeNull())
      await waitFor(() => expect(screen.getByTestId('popover-open')).toHaveTextContent('false'))
      expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    } finally {
      Storage.prototype.setItem = orig
    }

    expect(loadGoalDraftSnapshot(slot).draft).toBeNull()
  })
})
