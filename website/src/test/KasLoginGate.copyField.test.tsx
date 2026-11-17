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

describe('KasLoginGate device-code screen without a browser that opens by itself', () => {
  it('says the code did not copy, and keeps the sign-in attempt on screen', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    await openDeviceScreen()
    fireEvent.click(screen.getByRole('button', { name: 'Copy code' }))
    expect(await screen.findByText("Couldn't copy the code. Select it and copy it by hand.")).toBeTruthy()
    // Still the same attempt: its code and link are there, and Copy did not say Copied.
    expect(screen.getByTestId('kas-login-user-code').textContent).toBe('ABCD-1234')
    expect(screen.getByTestId('kas-login-verification-link')).toBeTruthy()
    expect(screen.getByTestId('kas-login-copy-code').textContent).toBe('Copy code')
    // A copy that works afterwards clears the notice.
    vi.mocked(copyToClipboard).mockResolvedValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))
    await waitFor(() => expect(screen.queryByTestId('kas-login-copy-error')).toBeNull())
  })
  it('shows the link as a link, a labelled Copy link, and the code in plain text with its own Copy', async () => {
    await openDeviceScreen()
    const link = screen.getByTestId('kas-login-verification-link')
    expect(link.tagName).toBe('A')
    expect(link.getAttribute('href')).toBe('https://example.com/verify?code=ABCD-1234')
    // A visible word, not only an icon's accessible name.
    expect(screen.getByTestId('kas-login-copy-link').textContent).toBe('Copy link')
    const code = screen.getByTestId('kas-login-user-code')
    expect(code.textContent).toBe('ABCD-1234')
    expect(code.className).toContain('select-all')

    fireEvent.click(screen.getByRole('button', { name: 'Copy code' }))
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith('ABCD-1234'))
    await waitFor(() => expect(screen.getByTestId('kas-login-copy-code').textContent).toBe('Copied'))
  })
})
