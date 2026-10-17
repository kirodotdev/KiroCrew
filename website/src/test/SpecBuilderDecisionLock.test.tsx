// A decision the user has answered is settled: its card must never offer the
// options again. Two ways it used to come back clickable — the agent's state file
// still reads `answer: null` for the round trip plus however long its turn takes,
// and a later state write can re-emit a settled decision id as pending, which
// reads on screen as a brand-new question rather than a re-render of an old one.
import { describe, it, expect, beforeEach } from 'vitest'
import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import SpecStatePanel from '../apps/spec-builder/components/SpecStatePanel'
import { type SpecDetail } from '../apps/spec-builder/api'

// The option controls are role=button with a complete, localizable action as
// their accessible name (Clickable + aria-label). Anchored on the option so a
// match cannot come from the card's surrounding text.
const HTTPS = /Answer “Inbound transport” with HTTPS \(recommended\)$/
const STREAMING = /Answer “Inbound transport” with Streaming$/

const PENDING: SpecDetail = {
  name: 's',
  state: {
    decisions: [
      { id: 'transport', title: 'Inbound transport', options: ['HTTPS', 'Streaming'], recommended: 'HTTPS' },
    ],
  },
}

let sent: [string, string, string][]
let resolveSend: (() => void) | undefined
let rejectSend: ((e: Error) => void) | undefined

function renderPanel(detail: SpecDetail = PENDING) {
  const answerDecision = (id: string, option: string, msg: string) => {
    sent.push([id, option, msg])
    return new Promise<void>((res, rej) => { resolveSend = () => res(); rejectSend = rej })
  }
  return render(<SpecStatePanel detail={detail} answerDecision={answerDecision} />)
}

/** An error shaped like the api client's: carries the backend's `code`. */
function refusal(code: string): Error & { code: string } {
  return Object.assign(new Error('refused'), { code })
}

beforeEach(() => { sent = []; resolveSend = undefined; rejectSend = undefined })

describe('decision cards are one-way', () => {
  it('locks the card on the first click, before the agent records anything', async () => {
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: HTTPS }))
    // The detail payload has not changed and the agent's state file still says
    // pending — the card must lock anyway, or a second click sends a second answer
    // for a decision the agent already has.
    await waitFor(() => expect(screen.queryByRole('button', { name: STREAMING })).not.toBeInTheDocument())
    expect(screen.queryByRole('button', { name: HTTPS })).not.toBeInTheDocument()
    // "sending", not "answered": the click is locked in locally but neither the server
    // nor the agent has acknowledged it yet, and the badge says so. What this test pins
    // is the LOCK (no options), which holds either way.
    expect(screen.getByText(/sending/)).toBeInTheDocument()
    expect(screen.getByText(/HTTPS/)).toBeInTheDocument()
    resolveSend?.()
    expect(sent).toHaveLength(1)
  })

  it('sends the decision id so the backend can refuse a second answer', () => {
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: HTTPS }))

    expect(sent[0][0]).toBe('transport')
    // The bare option, separate from the composed prompt: the backend records this
    // value and the card renders it back as the answer.
    expect(sent[0][1]).toBe('HTTPS')
    expect(sent[0][2]).toContain('HTTPS')
  })

  it('renders no options for a decision the backend reports as locked', () => {
    // The re-emitted card: the agent wrote this decision back as pending and the
    // backend overlaid its own record onto it. The client must key off `locked`
    // rather than the answer TEXT — that text is scrubbed on its way out of the
    // backend, and an unreadable answer must not re-open a settled decision.
    renderPanel({
      name: 's',
      state: {
        decisions: [
          {
            id: 'transport',
            title: 'Inbound transport',
            options: ['HTTPS', 'Streaming'],
            answer: '',
            locked: true,
          },
        ],
      },
    })

    expect(screen.queryByRole('button', { name: STREAMING })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: HTTPS })).not.toBeInTheDocument()
    expect(screen.getByText('answered')).toBeInTheDocument()
  })

  it('shows the recorded answer on a locked card', () => {
    renderPanel({
      name: 's',
      state: {
        decisions: [
          { id: 'transport', title: 'Inbound transport', options: ['HTTPS', 'Streaming'], answer: 'HTTPS', locked: true },
        ],
      },
    })

    expect(screen.getByText(/HTTPS/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: STREAMING })).not.toBeInTheDocument()
  })

  it('offers no options while the agent is working', () => {
    // An answer sent mid-turn is queued behind the running turn, and Pause clears
    // that queue — so the backend refuses it. Holding the options back means the
    // user meets a disabled control instead of an error.
    renderPanel({ ...PENDING, running: true })

    fireEvent.click(screen.getByRole('button', { name: HTTPS }))

    expect(sent).toHaveLength(0)
    expect(screen.getByRole('button', { name: HTTPS })).toHaveAttribute('aria-disabled', 'true')
  })

  it.each([
    ['decision_agent_busy', 'the agent was working'],
    ['spec_busy_elsewhere', 'another view of this spec was working'],
    ['decision_record_write_failed', 'the record could not be written'],
    ['decision_record_unreadable', 'the record could not be read'],
  ])('re-opens the card when the backend refused before recording anything (%s)', async (code) => {
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: HTTPS }))
    await waitFor(() => expect(screen.queryByRole('button', { name: STREAMING })).not.toBeInTheDocument())
    // Every one of these is emitted BEFORE anything is recorded, so the user must be
    // able to answer again — a locked card here would be permanent until reload.
    rejectSend?.(refusal(code))

    await waitFor(() => expect(screen.getByRole('button', { name: STREAMING })).toBeInTheDocument())
  })

  it('keeps the card locked when the failure could have committed', async () => {
    const view = renderPanel()

    fireEvent.click(screen.getByRole('button', { name: HTTPS }))
    await waitFor(() => expect(screen.queryByRole('button', { name: STREAMING })).not.toBeInTheDocument())
    // No code: a dropped connection. The write may well have landed, so re-opening the
    // card would invite a second answer for a decision the agent already has.
    rejectSend?.(new Error('network error'))

    await new Promise((r) => setTimeout(r, 0))
    expect(screen.queryByRole('button', { name: STREAMING })).not.toBeInTheDocument()
    // Still "sending": the request failed ambiguously, so the optimistic lock is kept
    // and the badge stays honest that nothing has confirmed the answer yet.
    expect(screen.getByText(/sending/)).toBeInTheDocument()

    // ...and the refetched detail is what decides. It reports the decision unlocked,
    // so the request never reached the server and the card must come back rather than
    // stay locked forever.
    view.rerender(<SpecStatePanel detail={{ ...PENDING }} answerDecision={() => new Promise<void>(() => {})} />)
    await waitFor(() => expect(screen.getByRole('button', { name: STREAMING })).toBeInTheDocument())
  })

  it('does not re-open a card the refetched detail reports as locked', async () => {
    const view = renderPanel()

    fireEvent.click(screen.getByRole('button', { name: HTTPS }))
    rejectSend?.(new Error('network error'))
    await new Promise((r) => setTimeout(r, 0))

    // The write DID commit; the refetch says so. The card stays settled.
    view.rerender(
      <SpecStatePanel
        detail={{
          name: 's',
          state: {
            decisions: [{ id: 'transport', title: 'Inbound transport', options: ['HTTPS', 'Streaming'], answer: 'HTTPS', locked: true }],
          },
        }}
        answerDecision={() => new Promise<void>(() => {})}
      />,
    )
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.queryByRole('button', { name: STREAMING })).not.toBeInTheDocument()
    expect(screen.getByText('answered')).toBeInTheDocument()
  })
})
