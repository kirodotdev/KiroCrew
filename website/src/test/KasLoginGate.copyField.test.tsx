/**
 * KasLoginGate's device-code screen renders a click-to-copy verification link
 * (`CopyField`). Its confirmation must gate on the shared helper's boolean
 * result, not on the write merely having been attempted.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import KasLoginGate from '../components/KasLoginGate'

vi.mock('../api/client', () => ({
  api: {
    kasLoginStatus: vi.fn().mockResolvedValue({
      authenticated: false, provider: null, identity: null, transport: 'device', expires_at: null,
    }),
    kasLoginBeginDevice: vi.fn().mockResolvedValue({
      login_id: 'login-1',
      user_code: 'ABCD-1234',
      verification_uri_complete: 'https://example.com/verify?code=ABCD-1234',
      expires_at: '2026-01-01T00:00:00Z',
    }),
    kasLoginBeginLoopback: vi.fn(),
    kasLoginPoll: vi.fn().mockResolvedValue({ status: 'pending' }),
    kasLoginCancel: vi.fn().mockResolvedValue({}),
  },
  ApiError: class ApiError extends Error {
    status: number
    body?: string
    constructor(status: number, message: string, body?: string) {
      super(message)
      this.status = status
      this.body = body
    }
  },
}))

vi.mock('../utils/clipboard', () => ({
  // Resolves TRUE by default: the real signature is `Promise<boolean>`, and a
  // bare `vi.fn()` returning undefined is falsy, so it could not tell a
  // successful clipboard write from a failed one.
  copyToClipboard: vi.fn().mockResolvedValue(true),
}))

import { copyToClipboard } from '../utils/clipboard'

async function openDeviceScreen() {
  renderWithProviders(<KasLoginGate />)
  const githubBtn = await screen.findByText('Continue with GitHub')
  fireEvent.click(githubBtn)
  await screen.findByTestId('kas-login-user-code')
}

beforeEach(() => {
  vi.mocked(copyToClipboard).mockReset().mockResolvedValue(true)
})

describe('KasLoginGate device verification link copy', () => {
  it('confirms only once the link actually reached the clipboard', async () => {
    await openDeviceScreen()
    const copyBtn = screen.getByRole('button', { name: 'Copy link' })
    fireEvent.click(copyBtn)

    await waitFor(() =>
      expect(copyToClipboard).toHaveBeenCalledWith('https://example.com/verify?code=ABCD-1234'),
    )
    await waitFor(() => expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy())
  })

  it('leaves the glyph alone when both clipboard paths fail', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    await openDeviceScreen()
    const copyBtn = screen.getByRole('button', { name: 'Copy link' })
    fireEvent.click(copyBtn)

    await waitFor(() => expect(copyToClipboard).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.queryByRole('button', { name: 'Copied' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Copy link' })).toBeTruthy()
  })
})
