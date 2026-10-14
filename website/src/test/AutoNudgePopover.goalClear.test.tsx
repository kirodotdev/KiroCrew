import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import {
  __resetForTests,
  loadGoalDraft,
  loadGoalDraftSnapshot,
  saveGoalDraft,
} from '../utils/goalDrafts'
import {
  isGoalRecordKey,
  makeLoop,
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
