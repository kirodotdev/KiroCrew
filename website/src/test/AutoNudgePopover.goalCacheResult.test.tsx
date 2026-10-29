import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { __resetForTests, saveGoalDraft } from '../utils/goalDrafts'
import { SLOT, isGoalRecordKey, renderPopover } from './autoNudgePopoverFixtures'

const testNowMs = () => Date.now() // flake-ok: relative stamps are compared synchronously within each test

/** The conflict notice: another device saved a newer goal; "Replace with my text" replaces it. */
const CONFLICT = 'A newer goal was saved on another device. Use it, or replace it with your text.'

const respond = (status: number, body: unknown) => ({ ok: status < 300, status, json: () => Promise.resolve(body) })

/**
 * How a live edit's server answer lands in the open editor once this browser has
 * tried to cache it: `superseded` (another tab saved a newer goal first) and
 * `retained` (the store confirmed the edit, but this browser could not cache it).
 */
describe('AutoNudgePopover live-edit answers by cache result', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const goalBox = () => screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i) as HTMLTextAreaElement
  const start = () => screen.getByRole('button', { name: /Start loop/i })

  it('blocks Start behind the conflict notice when another tab saved a newer goal before this save answered', async () => {
    const puts: Record<string, unknown>[] = []
    const answer: { put?: () => void } = {}
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) return Promise.resolve(respond(200, { jobs: [] }))
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        // The store commits this edit, but its answer is delayed.
        return new Promise(resolve => {
          answer.put = () => resolve(respond(200, { draft: sent.draft, updated_at: Number(sent.updated_at) + 1 }))
        })
      }
      return Promise.resolve(respond(200, { draft: null, updated_at: 0 }))
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    fireEvent.change(goalBox(), { target: { value: 'tab A goal' } })
    await waitFor(() => expect(puts).toHaveLength(1))

    // Another tab reconciles and saves a newer goal into the shared browser
    // storage before this tab's answer arrives, so caching that answer is superseded.
    saveGoalDraft(SLOT, { message: 'tab B newer goal', idleSecs: 90, maxCycles: 4 }, testNowMs() + 10_000)
    await act(async () => {
      answer.put?.()
      await Promise.resolve()
      await Promise.resolve()
    })

    // The editor still shows the older goal, so Start must not launch it.
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(CONFLICT))
    expect(goalBox().value).toBe('tab A goal')
    expect(start()).toBeDisabled()
  })

  it('clears a failed check once the store confirms an edit this browser cannot cache', async () => {
    const puts: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) return Promise.resolve(respond(200, { jobs: [] }))
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        return Promise.resolve(respond(200, { draft: sent.draft, updated_at: Number(sent.updated_at) + 1 }))
      }
      // The opening read of the empty slot fails, so the editor starts in the failed check.
      return Promise.resolve(respond(503, { error: 'temporarily unavailable' }))
    }) as unknown as typeof fetch)

    // Browser storage refuses every goal record for the whole visit.
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(k: string, v: string) {
      if (isGoalRecordKey(k)) throw new Error('QuotaExceeded')
      return orig.call(this, k, v)
    }
    try {
      renderPopover(null)
      await screen.findByTestId('goal-draft-sync-error')
      fireEvent.change(goalBox(), { target: { value: 'edit the store confirms' } })
      expect(start()).toBeDisabled()

      // The store confirms the edit; this browser keeps it only in memory.
      await waitFor(() => expect(puts).toHaveLength(1))
      expect((puts[0].draft as Record<string, unknown>).message).toBe('edit the store confirms')
      await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull())
      expect(goalBox().value).toBe('edit the store confirms')
      expect(start()).toBeEnabled()
    } finally {
      Storage.prototype.setItem = orig
    }
  })
})
