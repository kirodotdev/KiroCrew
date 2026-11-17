import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react'

const client = vi.hoisted(() => ({
  armLimitContinue: vi.fn<(slot: string, resetAt: string) => Promise<{ ok: boolean; warning?: string; continue_at?: string }>>(),
  cancelLimitContinue: vi.fn<(slot: string) => Promise<{ ok: boolean; cancelled: boolean; started?: boolean }>>(),
}))
vi.mock('../api/client', () => ({ api: client }))

import { ErrorCard } from '../pages/chat/ErrorCard'
import { fmtDateTime } from '../i18n/format'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import { createTestStore, renderWithProviders } from './helpers'

// Fixed instants far from now, so the test pins no clock: 2096 is ahead, 2001 is gone.
const AHEAD = '2096-10-02T09:46:40.000Z'
const GONE = '2001-09-09T01:46:40.000Z'
// The instant the gateway reports: the reset plus its one-minute margin.
const RESUME = '2096-10-02T09:47:40+00:00'
const PROSE = "You've hit your session limit \u00b7 resets soon."

const slotRow = (limitContinueAt?: string) => ({ key: 's1', messages: 1, running: false, ...(limitContinueAt ? { limit_continue_at: limitContinueAt } : {}) }) as ChatSlot

function card(props: Partial<Parameters<typeof ErrorCard>[0]> = {}, slot: ChatSlot = slotRow()) {
  const store = createTestStore()
  store.dispatch(sseSlots([slot]))
  const view = renderWithProviders(
    <ErrorCard content={PROSE} meta={{ kind: 'usage_limit', reset_at: AHEAD }} onContinue={() => undefined} limitContinueSlot="s1" {...props} />,
    { store },
  )
  return { store, ...view }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ErrorCard — continue automatically after a usage limit resets', () => {
  it('offers the button under the reset line, beside Resume and nothing else', () => {
    card()
    const time = fmtDateTime(Date.parse(AHEAD))
    expect(screen.getByTestId('error-card-limit-reset')).toHaveTextContent(`The limit resets on ${time}.`)
    expect(screen.getByTestId('error-card-limit-continue')).toHaveTextContent(`Continue automatically after ${time}`)
    expect(screen.getByTestId('error-card-continue')).toBeVisible()
    expect(screen.getByTestId('error-card').querySelectorAll('button')).toHaveLength(2)
    expect(screen.queryByTestId('error-card-limit-continue-armed')).toBeNull()
  })

  it.each([
    ['a row with no reset time', { kind: 'usage_limit' }],
    ['a reset already gone', { kind: 'usage_limit', reset_at: GONE }],
    ['a time that is not an instant', { kind: 'usage_limit', reset_at: 'soon' }],
  ])('is today’s card for %s', (_label, meta) => {
    card({ meta })
    expect(screen.queryByTestId('error-card-limit-continue')).toBeNull()
    expect(screen.getByTestId('error-card-continue')).toBeVisible()
  })

  it('is today’s card when the host names no chat or the row cannot be resumed', () => {
    card({ limitContinueSlot: undefined })
    expect(screen.queryByTestId('error-card-limit-continue')).toBeNull()
    cleanup()
    card({ onContinue: undefined })
    expect(screen.queryByTestId('error-card-limit-continue')).toBeNull()
  })

  it('arms the chat for the row’s instant, then offers Cancel and says when it will continue', async () => {
    client.armLimitContinue.mockResolvedValue({ ok: true, continue_at: RESUME })
    card()
    fireEvent.click(screen.getByTestId('error-card-limit-continue'))
    await waitFor(() => expect(screen.getByTestId('error-card-limit-continue')).toHaveAttribute('aria-pressed', 'true'))
    expect(client.armLimitContinue).toHaveBeenCalledWith('s1', AHEAD)
    expect(screen.getByTestId('error-card-limit-continue')).toHaveTextContent('Cancel automatic continue')
    expect(screen.getByTestId('error-card-limit-continue-armed')).toHaveTextContent(
      `This chat will continue once by itself at ${fmtDateTime(Date.parse(RESUME))}.`,
    )
    expect(screen.getByTestId('error-card').querySelectorAll('button')).toHaveLength(2)
  })

  it('cancels on the next press', async () => {
    client.armLimitContinue.mockResolvedValue({ ok: true })
    client.cancelLimitContinue.mockResolvedValue({ ok: true, cancelled: true })
    card()
    fireEvent.click(screen.getByTestId('error-card-limit-continue'))
    await waitFor(() => expect(screen.getByTestId('error-card-limit-continue')).toHaveAttribute('aria-pressed', 'true'))
    fireEvent.click(screen.getByTestId('error-card-limit-continue'))
    await waitFor(() => expect(screen.getByTestId('error-card-limit-continue')).toHaveAttribute('aria-pressed', 'false'))
    expect(client.cancelLimitContinue).toHaveBeenCalledWith('s1')
    expect(screen.queryByTestId('error-card-limit-continue-armed')).toBeNull()
  })

  it('says the continue already started when Cancel came after the gateway queued it', async () => {
    client.cancelLimitContinue.mockResolvedValue({ ok: true, cancelled: false, started: true })
    card({}, slotRow(RESUME))
    fireEvent.click(screen.getByTestId('error-card-limit-continue'))
    expect(await screen.findByTestId('error-card-limit-continue-started')).toHaveTextContent(
      'The automatic continue had already started, so it was not cancelled.',
    )
    expect(screen.queryByTestId('error-card-limit-continue')).toBeNull()
    expect(screen.queryByTestId('error-card-limit-continue-armed')).toBeNull()
  })

  it('sends the row’s own reset string back unchanged, microseconds and all', async () => {
    // A relative clause ("resets in 2 hours") gives the row the clock's microseconds.
    const micro = '2096-10-02T09:46:40.123456+00:00'
    client.armLimitContinue.mockResolvedValue({ ok: true })
    card({ meta: { kind: 'usage_limit', reset_at: micro } })
    fireEvent.click(screen.getByTestId('error-card-limit-continue'))
    await waitFor(() => expect(client.armLimitContinue).toHaveBeenCalledWith('s1', micro))
  })

  it.each([
    ['the reset has passed but the gateway’s margin has not', { meta: { kind: 'usage_limit', reset_at: GONE } }],
    ['the chat cannot be resumed right now', { onContinue: undefined }],
  ])('keeps Cancel while the gateway holds a wait and %s', async (_label, props) => {
    client.cancelLimitContinue.mockResolvedValue({ ok: true, cancelled: true })
    card(props, slotRow(RESUME))
    expect(screen.getByTestId('error-card-limit-continue-armed')).toBeVisible()
    expect(screen.getByTestId('error-card-limit-continue')).toHaveTextContent('Cancel automatic continue')
    fireEvent.click(screen.getByTestId('error-card-limit-continue'))
    await waitFor(() => expect(screen.queryByTestId('error-card-limit-continue')).toBeNull())
    expect(client.cancelLimitContinue).toHaveBeenCalledWith('s1')
    expect(screen.queryByTestId('error-card-limit-continue-armed')).toBeNull()
  })

  it('shows a wait the gateway already holds, as a reloaded card does', () => {
    card({}, slotRow(RESUME))
    expect(screen.getByTestId('error-card-limit-continue')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('error-card-limit-continue-armed')).toHaveTextContent(
      `This chat will continue once by itself at ${fmtDateTime(Date.parse(RESUME))}.`,
    )
    expect(screen.getByTestId('error-card-limit-continue-armed')).not.toHaveTextContent(fmtDateTime(Date.parse(AHEAD)))
  })

  it('follows the gateway when its summary changes after a press', async () => {
    client.armLimitContinue.mockResolvedValue({ ok: true })
    const { store } = card()
    fireEvent.click(screen.getByTestId('error-card-limit-continue'))
    await waitFor(() => expect(screen.getByTestId('error-card-limit-continue')).toHaveAttribute('aria-pressed', 'true'))
    act(() => { store.dispatch(sseSlots([slotRow(RESUME)])) })
    expect(screen.getByTestId('error-card-limit-continue')).toHaveAttribute('aria-pressed', 'true')
    act(() => { store.dispatch(sseSlots([slotRow()])) })
    expect(screen.getByTestId('error-card-limit-continue')).toHaveAttribute('aria-pressed', 'false')
  })

  it.each([
    ['declines with a warning', () => client.armLimitContinue.mockResolvedValue({ ok: false, warning: 'that reset time has already passed' })],
    ['cannot be reached', () => client.armLimitContinue.mockRejectedValue(new Error('offline'))],
  ])('says so when the gateway %s, and stays unarmed', async (_label, arrange) => {
    arrange()
    card()
    fireEvent.click(screen.getByTestId('error-card-limit-continue'))
    expect(await screen.findByTestId('error-card-limit-continue-failed')).toHaveTextContent('Could not set up the automatic continue')
    expect(screen.getByTestId('error-card-limit-continue')).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByTestId('error-card-continue')).toBeVisible()
  })
})
