import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { api } from '../api/client'

// A bot's own thread draws no session environment chip (its workers' environment
// is a choice on the bot's Settings page), but its placement is still read: a bot
// thread already on a profile must not offer this machine's project picker,
// whose pick would change the gateway's project, not the workspace it runs in.

vi.mock('../components/SessionEnvironmentChip', () => ({
  default: () => <div data-testid="environment-chip" />,
}))

const CODER = { id: 'cdr', name: 'Coder box', provider: 'coder', workdir: '/workspace' }
/** A named ceiling for the lazy chip's import and render (see the chip case). */
const LAZY_CHIP_TIMEOUT_MS = 5000
const props = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  onProjectClick: vi.fn(),
  project: '/srv/repos/KiroCrew',
  environmentSlot: 'member-kiro',
}
const projectChip = () => screen.queryByRole('button', { name: /Project: |Select project/ })

beforeEach(() => { vi.restoreAllMocks() })

describe('ChatInput with the environment chip hidden', () => {
  it('draws no chip, and still hides the local project picker for a remote chat', async () => {
    const read = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ profile: 'cdr', provider: 'coder', profiles: [CODER] })
    renderWithProviders(<ChatInput {...props} environmentChipHidden />)
    await waitFor(() => expect(read).toHaveBeenCalledWith('member-kiro'))
    await waitFor(() => expect(projectChip()).toBeNull())
    expect(screen.queryByTestId('environment-chip')).toBeNull()
  })

  it('keeps the project picker for a chat on this machine', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ profile: null, profiles: [CODER] })
    renderWithProviders(<ChatInput {...props} environmentChipHidden />)
    expect(await screen.findByRole('button', { name: /Project: / })).toBeInTheDocument()
    expect(screen.queryByTestId('environment-chip')).toBeNull()
  })

  it('draws the chip when it is not hidden', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ profile: null, profiles: [CODER] })
    renderWithProviders(<ChatInput {...props} />)
    // The chip loads through ChatInput's lazy chip chain (lazyChips -> lazyRetry
    // -> React.lazy(import)), so the wait gets a named ceiling for that import
    // and its render on a loaded worker, not the default one second.
    expect(await screen.findByTestId('environment-chip', {}, { timeout: LAZY_CHIP_TIMEOUT_MS })).toBeInTheDocument()
  })
})
