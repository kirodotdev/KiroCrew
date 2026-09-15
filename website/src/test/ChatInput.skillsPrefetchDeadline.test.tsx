import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skillTrust: vi.fn(),
  grantSkillTrust: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import ChatInput from '../components/ChatInput'

beforeEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  localStorage.clear()
  mockApi.skillTrust.mockResolvedValue({ project: '/work/p', project_key: '/work/p' })
  mockApi.grantSkillTrust.mockResolvedValue({ trusted: true })
})
afterEach(() => { vi.restoreAllMocks() })

describe('ChatInput — the focus prefetch goes through the bounded client', () => {
  it('hands api.skills an AbortSignal on focus', async () => {
    // Shares the menu's query key by design, so react-query dedupes and the
    // deadline lives in the client; this site owes only the signal.
    mockApi.skills.mockImplementation(() => new Promise(() => {}))
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    fireEvent.focus(screen.getByLabelText('Message input'))
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    expect(mockApi.skills.mock.calls[0][2]).toBeInstanceOf(AbortSignal)
  })

  it('does not leave the prefetch pending once it settles', async () => {
    mockApi.skills.mockImplementation(() =>
      new Promise((_res, rej) =>
        setTimeout(() => rej(new DOMException('deadline exceeded', 'TimeoutError')), 5)))
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    fireEvent.focus(screen.getByLabelText('Message input'))
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    // Settling is the whole point; an unhandled rejection here would fail the run.
    await new Promise(r => setTimeout(r, 30))
  })
})
