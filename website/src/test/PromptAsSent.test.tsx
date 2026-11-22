/**
 * "Prompt as sent", under the Context tab's selected turn: closed it reads nothing;
 * opened it shows the turn's text, or says plainly why there is none.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'

import { ApiError } from '../api/apiError'
import { PromptAsSent } from '../pages/PromptAsSent'
import { renderWithProviders } from './helpers'

const promptMock = vi.fn()
vi.mock('../api/client', () => ({
  api: { telemetryPromptAsSent: (slot: string, turn: number) => promptMock(slot, turn) },
}))

afterEach(() => {
  cleanup()
  promptMock.mockReset()
})

const refusal = (status: number, code: string) => new ApiError(status, code, JSON.stringify({ error: code, code }))

function open() {
  renderWithProviders(<PromptAsSent slot="chat-1" turn={3} version="v1" />)
  fireEvent.click(screen.getByRole('button', { name: 'Prompt as sent' }))
}

describe('PromptAsSent', () => {
  it('reads nothing until it is opened, then shows the turn as sent', async () => {
    promptMock.mockResolvedValue({ slot: 'chat-1', turn: 3, text: 'rules first\nthen the ask' })
    renderWithProviders(<PromptAsSent slot="chat-1" turn={3} version="v1" />)
    expect(promptMock).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Prompt as sent' }))
    const text = await screen.findByTestId('prompt-as-sent-text')
    expect(text.textContent).toBe('rules first\nthen the ask')
    expect(promptMock).toHaveBeenCalledWith('chat-1', 3)
    expect(screen.getByText('The text this turn sent the agent, with secrets masked.')).toBeInTheDocument()
  })

  it('re-reads an open prompt when its turn is sent again', async () => {
    promptMock.mockResolvedValue({ slot: 'chat-1', turn: 3, text: 'first attempt' })
    // The app's own client never goes stale on its own.
    const { rerender } = renderWithProviders(<PromptAsSent slot="chat-1" turn={3} version="v1" />, {
      queryDefaults: { staleTime: Infinity },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Prompt as sent' }))
    expect((await screen.findByTestId('prompt-as-sent-text')).textContent).toBe('first attempt')
    promptMock.mockResolvedValue({ slot: 'chat-1', turn: 3, text: 'second attempt' })
    rerender(<PromptAsSent slot="chat-1" turn={3} version="v2" />)
    await waitFor(() => expect(screen.getByTestId('prompt-as-sent-text').textContent).toBe('second attempt'))
  })

  it('never shows an earlier attempt while the turn is sent again', async () => {
    promptMock.mockResolvedValue({ slot: 'chat-1', turn: 3, text: 'first attempt' })
    // `gone`: the turn's row is not kept yet, as while the turn runs.
    const { rerender } = renderWithProviders(<PromptAsSent slot="chat-1" turn={3} version="gone" />, {
      queryDefaults: { staleTime: Infinity },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Prompt as sent' }))
    expect((await screen.findByTestId('prompt-as-sent-text')).textContent).toBe('first attempt')
    rerender(<PromptAsSent slot="chat-1" turn={3} version="v1" />)
    await waitFor(() => expect(promptMock).toHaveBeenCalledTimes(2))
    expect((await screen.findByTestId('prompt-as-sent-text')).textContent).toBe('first attempt')
    promptMock.mockResolvedValue({ slot: 'chat-1', turn: 3, text: 'second attempt' })
    rerender(<PromptAsSent slot="chat-1" turn={3} version="gone" />)
    await waitFor(() => expect(screen.getByTestId('prompt-as-sent-text').textContent).toBe('second attempt'))
  })

  it('says the prompt is no longer kept', async () => {
    promptMock.mockRejectedValue(refusal(404, 'not_kept'))
    open()
    expect((await screen.findByTestId('prompt-as-sent-unavailable')).textContent).toMatch(/^This turn's prompt is no longer kept\./)
  })

  it('says the prompt was too long to keep, not that it left the kept turns', async () => {
    promptMock.mockRejectedValue(refusal(404, 'too_long'))
    open()
    expect((await screen.findByTestId('prompt-as-sent-unavailable')).textContent).toBe("This turn's prompt was too long to keep, so it can't be shown.")
  })

  it('names the prompt, not the context breakdown, while it loads', async () => {
    promptMock.mockReturnValue(new Promise(() => {}))
    open()
    expect(await screen.findByText('Loading the prompt…')).toBeInTheDocument()
  })

  it('tells anyone but the dashboard owner that only the owner can read it, as a refusal', async () => {
    promptMock.mockRejectedValue(refusal(403, 'owner_only'))
    open()
    const notice = await screen.findByRole('alert')
    expect(within(notice).getByText("Only the dashboard owner can read a turn's prompt.")).toBeInTheDocument()
    expect(within(notice).getByText('owner_only')).toBeInTheDocument()
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
    expect(screen.queryByTestId('prompt-as-sent-unavailable')).toBeNull()
  })
})
