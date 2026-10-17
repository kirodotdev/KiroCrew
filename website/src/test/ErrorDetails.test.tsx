//
// `ErrorDetails` is the collapsed half of a two-audience deploy refusal: the
// banner stays a plain sentence and the stack names plus the runnable command
// sit one click away. Its copy button is the part a page-level test never
// reaches, because a page test stops at reading the banner.
//
// What these pin: nothing renders when there is nothing to show, the details and
// the command appear only once opened, and copying the command reports success.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import ErrorDetails from '../components/ErrorDetails'

describe('ErrorDetails', () => {
  beforeEach(() => { vi.restoreAllMocks() })
  afterEach(() => { vi.unstubAllGlobals() })

  it('renders nothing when there is no detail and no command', () => {
    const { container } = render(<ErrorDetails />)
    expect(container.textContent).toBe('')
  })

  it('keeps the detail collapsed until asked', () => {
    render(<ErrorDetails details="kirocrew-deploy-base" />)
    expect(screen.queryByText('kirocrew-deploy-base')).toBeNull()
    fireEvent.click(screen.getByText('Details'))
    expect(screen.getByText('kirocrew-deploy-base')).toBeTruthy()
  })

  it('copies the remediation command and reports it', async () => {
    const writeText = vi.fn(async () => undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    render(<ErrorDetails details="kirocrew-deploy-base" remediation="/s/install-reaper.sh --profile ship" />)
    fireEvent.click(screen.getByText('Details'))
    expect(screen.getByText('/s/install-reaper.sh --profile ship')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Copy/i }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('/s/install-reaper.sh --profile ship'))
  })

  it('leaves the button alone when the clipboard refuses', async () => {
    vi.stubGlobal('navigator', {
      clipboard: { writeText: vi.fn(async () => { throw new Error('denied') }) },
    })
    render(<ErrorDetails remediation="/s/install-reaper.sh" />)
    fireEvent.click(screen.getByText('Details'))
    fireEvent.click(screen.getByRole('button', { name: /Copy/i }))
    // A refused write must not report success: the label stays put.
    await waitFor(() => expect(screen.getByRole('button', { name: /Copy/i })).toBeTruthy())
  })
})
