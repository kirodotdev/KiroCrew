import { render, screen, fireEvent } from '@testing-library/react'
import AskAgentButton, { askAgentPrompt, askAgentHard } from './AskAgentButton'
import {
  recordError,
  sendErrorToChat,
  __resetErrorJournalForTests,
  type ErrorReport,
} from '../utils/errorReport'

vi.mock('../utils/errorReport', async importOriginal => {
  const mod = await importOriginal<typeof import('../utils/errorReport')>()
  return { ...mod, sendErrorToChat: vi.fn() }
})

const send = vi.mocked(sendErrorToChat)

function report(over: Partial<ErrorReport> = {}): ErrorReport {
  return { id: 'zzq-id', at: 1, source: 'api', message: 'zzq-msg', route: '/zzq', ...over }
}

describe('AskAgentButton', () => {
  beforeEach(() => {
    __resetErrorJournalForTests()
    send.mockReset()
  })

  it('renders nothing when it has neither a report nor a message', () => {
    const { container } = render(<AskAgentButton />)
    expect(container.firstChild).toBeNull()
  })

  it('resolves the journal entry at click time, not at render time', () => {
    render(<AskAgentButton message="zzq-boom-1" />)

    // Journalled AFTER render — the pre-journal render must not have captured it.
    recordError({ message: 'zzq-boom-1', source: 'api', detail: 'zzq-detail-1' })

    fireEvent.click(screen.getByRole('button'))
    expect(send).toHaveBeenCalledTimes(1)
    const prompt = send.mock.calls[0][0]
    expect(prompt).toContain('zzq-boom-1')
    expect(prompt).toContain('zzq-detail-1')
    expect(send.mock.calls[0][1]).toEqual({ hard: false })
  })

  it('falls back to the bare message when the journal has no entry', () => {
    render(<AskAgentButton message="zzq-unjournalled" />)
    fireEvent.click(screen.getByRole('button'))
    expect(send.mock.calls[0][0]).toContain('zzq-unjournalled')
  })

  it('prefers an explicit report over the journal and forwards hard', () => {
    render(<AskAgentButton report={report({ message: 'zzq-explicit' })} hard variant="solid" />)
    fireEvent.click(screen.getByRole('button'))
    expect(send.mock.calls[0][0]).toContain('zzq-explicit')
    expect(send.mock.calls[0][1]).toEqual({ hard: true })
  })

  it('the solid variant carries the accent skin, the link variant the danger skin', () => {
    const { unmount } = render(<AskAgentButton message="zzq-a" variant="solid" />)
    expect(screen.getByRole('button').className).toContain('bg-accent')
    unmount()
    render(<AskAgentButton message="zzq-a" />)
    expect(screen.getByRole('button').className).toContain('text-danger/80')
  })

  it('askAgentPrompt embeds the message', () => {
    expect(askAgentPrompt({ message: 'zzq-prompt-only' })).toContain('zzq-prompt-only')
  })

  it('askAgentHard resolves the journal itself and forces a hard handoff', () => {
    recordError({ message: 'zzq-hard', source: 'api', detail: 'zzq-hard-detail' })
    askAgentHard('zzq-hard')
    expect(send).toHaveBeenCalledTimes(1)
    expect(send.mock.calls[0][0]).toContain('zzq-hard-detail')
    expect(send.mock.calls[0][1]).toEqual({ hard: true })
  })
})
