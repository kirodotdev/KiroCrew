import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { __resetForTests, saveGoalDraft } from '../utils/goalDrafts'
import { SLOT, makeLoop, renderPopover } from './autoNudgePopoverFixtures'

const testNowMs = () => Date.now() // flake-ok: relative stamps are compared synchronously within the test

const CONFLICT = 'A newer goal was saved on another device. Use it, or replace it with your text.'
const FAILED = "Couldn't sync this draft to other devices. Your copy here is saved. Retry to sync."

type Answer = { ok: boolean, status?: number, json: () => Promise<unknown> }

/** A server whose draft PUTs wait for the test to answer them, and which
 *  records every create (`POST /api/autonudge`). */
function stubServer(stamp: number) {
  const puts: Record<string, unknown>[] = []
  const creates: Record<string, unknown>[] = []
  const answers: Array<(answer: Answer) => void> = []
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (String(url).startsWith('/api/crons')) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
    }
    if (String(url) === '/api/autonudge' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      creates.push(body)
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: makeLoop({ message: String(body.message) }) }) })
    }
    if (init?.method === 'PUT') {
      puts.push(JSON.parse(String(init.body)) as Record<string, unknown>)
      return new Promise(resolve => { answers.push(resolve) })
    }
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({
        draft: { message: 'original shared goal', idle_secs: 60, max_cycles: 0 },
        updated_at: stamp,
      }),
    })
  }) as unknown as typeof fetch)
  return { puts, creates, answers }
}

describe('AutoNudgePopover Start waits for a pending draft save', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const goalBox = () => screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i) as HTMLTextAreaElement
  const startButton = () => screen.getByRole('button', { name: /Start loop/i })

  /** Open the shared draft, then edit it and press Start before the debounced save goes out. */
  async function editAndStart(stamp: number) {
    saveGoalDraft(SLOT, { message: 'original shared goal', idleSecs: 60, maxCycles: 0 }, stamp)
    const server = stubServer(stamp)
    renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    fireEvent.change(goalBox(), { target: { value: 'my stale edit' } })
    fireEvent.click(startButton())
    // Start sends the debounced save at once instead of waiting it out.
    await waitFor(() => expect(server.puts).toHaveLength(1))
    return server
  }

  it('creates no loop when the save Start waits on is refused as a conflict', async () => {
    const stamp = testNowMs()
    const { creates, answers } = await editAndStart(stamp)
    expect(creates).toHaveLength(0)
    await act(async () => {
      answers[0]({
        ok: false,
        status: 409,
        json: () => Promise.resolve({
          code: 'goal_draft_conflict',
          error: 'goal draft changed since it was loaded',
          current: {
            draft: { message: 'goal saved on another device', idle_secs: 60, max_cycles: 0 },
            updated_at: stamp + 100,
          },
        }),
      })
    })
    expect(await screen.findByTestId('goal-draft-sync-error')).toHaveTextContent(CONFLICT)
    expect(creates).toHaveLength(0)
    expect(startButton()).toBeDisabled()
    expect(goalBox().value).toBe('my stale edit')
  })

  it('creates no loop when the save Start waits on fails', async () => {
    const { creates, answers } = await editAndStart(testNowMs())
    await act(async () => {
      answers[0]({ ok: false, status: 500, json: () => Promise.resolve({ error: 'store unavailable' }) })
    })
    expect(await screen.findByTestId('goal-draft-sync-error')).toHaveTextContent(FAILED)
    expect(creates).toHaveLength(0)
    expect(startButton()).toBeDisabled()
  })

  it('shows Start waiting with the form read-only, then creates the loop from the saved text', async () => {
    const stamp = testNowMs()
    const { puts, creates, answers } = await editAndStart(stamp)
    expect(screen.getByTestId('goal-draft-sync-status')).toHaveTextContent('Saving…')
    expect(startButton()).toBeDisabled()
    expect(goalBox()).toBeDisabled()
    expect(creates).toHaveLength(0)
    await act(async () => {
      answers[0]({ ok: true, json: () => Promise.resolve({ draft: puts[0].draft, updated_at: stamp + 1 }) })
    })
    await waitFor(() => expect(creates).toHaveLength(1))
    expect(creates[0]).toMatchObject({ slot_key: SLOT, message: 'my stale edit', idle_secs: 60, max_cycles: 0 })
    // The confirmed save is the draft Start submitted: nothing is written again.
    expect(puts).toHaveLength(1)
  })

  it('starts at once with no pending save', async () => {
    const stamp = testNowMs()
    saveGoalDraft(SLOT, { message: 'original shared goal', idleSecs: 60, maxCycles: 0 }, stamp)
    const { puts, creates } = stubServer(stamp)
    renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    fireEvent.click(startButton())
    await waitFor(() => expect(creates).toHaveLength(1))
    expect(creates[0]).toMatchObject({ message: 'original shared goal' })
    expect(puts).toHaveLength(0)
  })
})
