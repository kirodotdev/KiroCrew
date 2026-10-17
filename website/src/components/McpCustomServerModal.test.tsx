import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { renderWithProviders } from '../test/helpers'
import McpCustomServerModal from './McpCustomServerModal'

vi.mock('../api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      mcpCustomAdd: vi.fn(),
      mcpCustomGet: vi.fn(),
      mcpCustomUpdate: vi.fn(),
    },
  }
})

const SERVERS = JSON.stringify({ mcpServers: { weather: { command: 'npx', args: ['weather'] } } })

async function addOne(onClose: () => void, askAgent = true) {
  renderWithProviders(<McpCustomServerModal open onClose={onClose} askAgent={askAgent} />)
  const textarea = document.querySelector('textarea')
  expect(textarea).not.toBeNull()
  fireEvent.change(textarea!, { target: { value: SERVERS } })
  fireEvent.click(await screen.findByRole('button', { name: /^Add/ }))
  await waitFor(() => expect(api.mcpCustomAdd).toHaveBeenCalledTimes(1))
}

describe('McpCustomServerModal', () => {
  beforeEach(() => vi.clearAllMocks())

  it('closes after an add that reached the agent spec', async () => {
    vi.mocked(api.mcpCustomAdd).mockResolvedValue({
      ok: true,
      added: ['weather'],
      enabled: false,
      agent_spec_rewritten: true,
    })
    const onClose = vi.fn()
    await addOne(onClose)
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(screen.queryByTestId('agent-spec-not-rewritten')).not.toBeInTheDocument()
  })

  it('stays open and says so when the add did not reach the agent spec', async () => {
    vi.mocked(api.mcpCustomAdd).mockResolvedValue({
      ok: true,
      added: ['weather'],
      enabled: false,
      agent_spec_rewritten: false,
    })
    const onClose = vi.fn()
    await addOne(onClose)
    expect(await screen.findByTestId('agent-spec-not-rewritten')).toHaveTextContent(
      'Saved, but this copy of Kiro Crew could not update the agent config',
    )
    expect(onClose).not.toHaveBeenCalled()
    // The server is already saved: a second Add would only collide with it.
    expect(screen.queryByRole('button', { name: /^Add/ })).not.toBeInTheDocument()
    // The server is saved, so the way out is Close, not Cancel.
    expect(screen.getAllByRole('button', { name: 'Close' }).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
  })

  it('drops the hand-off when the host holds unsaved state', async () => {
    vi.mocked(api.mcpCustomAdd).mockResolvedValue({
      ok: true,
      added: ['weather'],
      enabled: false,
      agent_spec_rewritten: false,
    })
    await addOne(vi.fn(), false)
    const notice = await screen.findByTestId('agent-spec-not-rewritten')
    expect(within(notice).queryByRole('button')).not.toBeInTheDocument()
  })
})
