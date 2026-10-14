import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { __resetForTests, saveGoalDraft } from '../utils/goalDrafts'
import { SLOT, renderPopover } from './autoNudgePopoverFixtures'

const testNowMs = () => Date.now() // flake-ok: relative stamps are compared synchronously within the test

/** The conflict notice: another device saved a newer goal; "Replace with my text" replaces it. */
const CONFLICT = 'A newer goal was saved on another device. Use it, or replace it with your text.'

describe('AutoNudgePopover goal conflict during typing', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const goalBox = () => screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i) as HTMLTextAreaElement

  it('a conflict answered after the user typed again still blocks Start', async () => {
    const originalStamp = testNowMs()
    saveGoalDraft(SLOT, { message: 'original shared goal', idleSecs: 60, maxCycles: 0 }, originalStamp)
    const puts: Record<string, unknown>[] = []
    const answers: Array<() => void> = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        puts.push(JSON.parse(String(init.body)) as Record<string, unknown>)
        // Another device saved a newer goal after this editor read the store, so
        // every write on this editor's base is rejected. Each answer waits for the
        // test, which lets the user type again before the first rejection lands.
        return new Promise(resolve => {
          answers.push(() => resolve({
            ok: false,
            status: 409,
            json: () => Promise.resolve({
              code: 'goal_draft_conflict',
              error: 'goal draft changed since it was loaded',
              current: {
                draft: { message: 'goal saved on another device', idle_secs: 60, max_cycles: 0 },
                updated_at: originalStamp + 100,
              },
            }),
          }))
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'original shared goal', idle_secs: 60, max_cycles: 0 },
          updated_at: originalStamp,
        }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    fireEvent.change(goalBox(), { target: { value: 'my edit' } })
    await waitFor(() => expect(puts).toHaveLength(1))

    // The user keeps typing before the rejection of the first autosave arrives.
    fireEvent.change(goalBox(), { target: { value: 'my edit, then more' } })
    await act(async () => { answers[0]() })

    // The editor still writes from the rejected base: the conflict shows at once,
    // and Start stays blocked instead of arming the loop from that base.
    const notice = await screen.findByTestId('goal-draft-sync-error')
    expect(notice).toHaveTextContent(CONFLICT)
    expect(screen.getByRole('button', { name: /Start loop/i })).toBeDisabled()
    expect(goalBox().value).toBe('my edit, then more')
  })
})
