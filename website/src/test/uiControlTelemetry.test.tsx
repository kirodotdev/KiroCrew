import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

const report = vi.fn()
// What the mocked request does, swapped per test: a plain function, so a
// rejection or throw is the request's own failure and not a mock's.
let behave: () => unknown = () => undefined
vi.mock('../api/client', () => ({
  api: {
    reportUiControlClick: (control: string) => {
      report(control)
      return behave()
    },
  },
}))

import { reportUiControlClick } from '../lib/uiControlTelemetry'
import FollowUpCard from '../components/FollowUpCard'

describe('reportUiControlClick', () => {
  beforeEach(() => {
    report.mockReset()
    behave = () => Promise.resolve(undefined)
  })

  it('sends only the control kind', () => {
    reportUiControlClick('option_chip')
    expect(report).toHaveBeenCalledWith('option_chip')
  })

  it('swallows a rejected request', async () => {
    let caught = false
    behave = () => ({
      then: (_ok: unknown, fail: (e: unknown) => void) => {
        caught = true
        fail('offline')
      },
    })
    expect(() => reportUiControlClick('followup_skip')).not.toThrow()
    await new Promise(r => setTimeout(r, 0))
    expect(caught).toBe(true)
  })

  it('swallows a synchronous throw', () => {
    behave = () => {
      throw 'boom'
    }
    expect(() => reportUiControlClick('option_chip')).not.toThrow()
  })
})

describe('FollowUpCard reports its button clicks', () => {
  beforeEach(() => {
    report.mockReset()
    behave = () => Promise.resolve(undefined)
  })

  const item = { title: 'Do the next thing', description: 'why', prompt: 'p' }

  function renderCard(onSkip = vi.fn(), onAddToSession = vi.fn()) {
    render(
      <FollowUpCard
        items={[item] as never}
        onAddToSession={onAddToSession}
        onStartInWorktree={vi.fn().mockResolvedValue(undefined)}
        onStartInNewChat={vi.fn().mockResolvedValue(undefined)}
        onSkip={onSkip}
        {...({} as Record<string, unknown>)}
      />,
    )
    return { onSkip, onAddToSession }
  }

  it('skip reports followup_skip and still skips', () => {
    const { onSkip } = renderCard()
    const skip = screen.getAllByRole('button').find(b => b.getAttribute('title')?.length && /dismiss/i.test(b.getAttribute('title') || ''))
    expect(skip).toBeTruthy()
    fireEvent.click(skip!)
    expect(report).toHaveBeenCalledWith('followup_skip')
    expect(onSkip).toHaveBeenCalledWith(0)
  })
})
