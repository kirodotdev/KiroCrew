import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import PrivacyNotice, { PRIVACY_NOTICE_STORAGE_KEY } from '../components/PrivacyNotice'

const HEARTBEAT_FIELDS = [
  'random installation ID',
  'app version',
  'release channel',
  'operating system',
  'CPU architecture',
  'Python minor version',
  'installation channel',
  'governance posture',
  'first-run flag',
] as const

function renderNotice() {
  return render(
    <MemoryRouter>
      <PrivacyNotice />
      <main id="main-content" tabIndex={-1}>Dashboard content</main>
    </MemoryRouter>,
  )
}

describe('PrivacyNotice', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('renders once as a labelled, non-modal region with privacy details', () => {
    renderNotice()

    const notice = screen.getByRole('region', { name: 'Privacy at a glance' })
    expect(notice).toHaveAttribute('aria-describedby', 'privacy-notice-description')
    expect(notice).not.toHaveAttribute('aria-modal')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Privacy details' })).toHaveAttribute(
      'href',
      '/settings?tab=privacy',
    )
    expect(screen.getByRole('link', { name: 'Privacy details' })).not.toHaveFocus()
    expect(screen.getByRole('button', { name: 'Dismiss' })).not.toHaveFocus()
  })

  it('names every field in the fixed nine-field heartbeat payload', () => {
    renderNotice()

    const description = document.getElementById('privacy-notice-description')
    expect(description).not.toBeNull()
    expect(HEARTBEAT_FIELDS).toHaveLength(9)
    for (const field of HEARTBEAT_FIELDS) {
      expect(description).toHaveTextContent(field)
    }
  })

  it('dismisses from the keyboard and persists the first-run marker', async () => {
    const user = userEvent.setup()
    renderNotice()

    await user.tab()
    expect(screen.getByRole('link', { name: 'Privacy details' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: 'Dismiss' })).toHaveFocus()
    await user.keyboard('{Enter}')

    expect(screen.queryByTestId('privacy-notice')).not.toBeInTheDocument()
    expect(localStorage.getItem(PRIVACY_NOTICE_STORAGE_KEY)).toBe('1')
  })

  it('moves focus to the main landmark after explicit dismissal', async () => {
    const user = userEvent.setup()
    renderNotice()

    await user.click(screen.getByRole('button', { name: 'Dismiss' }))

    await waitFor(() => expect(screen.getByRole('main')).toHaveFocus())
  })

  it('stays hidden after it has been dismissed', () => {
    localStorage.setItem(PRIVACY_NOTICE_STORAGE_KEY, '1')
    renderNotice()
    expect(screen.queryByTestId('privacy-notice')).not.toBeInTheDocument()
  })

  it('shows the disclosure when persisted state cannot be read', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })

    renderNotice()
    expect(screen.getByRole('region', { name: 'Privacy at a glance' })).toBeInTheDocument()
  })

  it('never blocks the current session when persistence is unavailable', async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    const user = userEvent.setup()
    renderNotice()

    await user.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('privacy-notice')).not.toBeInTheDocument()
  })
})
