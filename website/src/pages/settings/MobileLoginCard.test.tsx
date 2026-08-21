import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

vi.mock('../../api/client', () => ({
  api: { mobileLoginLink: vi.fn() },
}))

import { api } from '../../api/client'
import { MobileLoginCard } from './MobileLoginCard'

describe('MobileLoginCard', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('creates and copies the configured external dashboard link', async () => {
    const mobileLink = 'https://dashboard.example/?token=abc.def'
    ;(api.mobileLoginLink as ReturnType<typeof vi.fn>).mockResolvedValue({ url: mobileLink })
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.spyOn(navigator.clipboard, 'writeText').mockImplementation(writeText)

    renderWithProviders(<MobileLoginCard />)
    fireEvent.click(screen.getByRole('button', { name: 'Create mobile sign-in link' }))

    const link = await screen.findByLabelText('Mobile sign-in link')
    expect(link).toHaveValue(mobileLink)

    fireEvent.click(screen.getByRole('button', { name: 'Copy sign-in link' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(mobileLink))
    expect(await screen.findByRole('status')).toHaveTextContent('Link copied')
  })

  it('shows a retryable error when the dashboard cannot mint the link', async () => {
    ;(api.mobileLoginLink as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('offline'))

    renderWithProviders(<MobileLoginCard />)
    fireEvent.click(screen.getByRole('button', { name: 'Create mobile sign-in link' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not create a sign-in link. Try again.',
    )
  })

  it('explains how to copy manually when clipboard access is unavailable', async () => {
    ;(api.mobileLoginLink as ReturnType<typeof vi.fn>).mockResolvedValue({
      url: 'https://dashboard.example/?token=abc.def',
    })
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('denied'))
    Object.defineProperty(document, 'execCommand', {
      value: vi.fn().mockReturnValue(false),
      configurable: true,
    })

    renderWithProviders(<MobileLoginCard />)
    fireEvent.click(screen.getByRole('button', { name: 'Create mobile sign-in link' }))
    await screen.findByLabelText('Mobile sign-in link')
    fireEvent.click(screen.getByRole('button', { name: 'Copy sign-in link' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Copy failed. Select the link and copy it manually.',
    )
  })
})
