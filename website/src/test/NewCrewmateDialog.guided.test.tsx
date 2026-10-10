/**
 * The guided door of the New crewmate card: the `crewmate.create` guide's
 * Create step completes from what the gateway created, so the card's create
 * carries the guide's headers, waits for a report still in flight, and tells
 * the guide when a save was refused or went through without it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import NewCrewmateDialog from '../pages/members/NewCrewmateDialog'

const guide = vi.hoisted(() => ({
  headers: undefined as Record<string, string> | undefined,
  sync: vi.fn(() => Promise.resolve(true)),
  refused: vi.fn(),
  savedUncredited: vi.fn(),
}))
vi.mock('../guide/GuideContext', async importOriginal => {
  const mod = await importOriginal<typeof import('../guide/GuideContext')>()
  return {
    ...mod,
    useGuideRequestHeaders: () => () => guide.headers,
    useGuideSaveLifecycle: () => ({ sync: guide.sync, refused: guide.refused, savedUncredited: guide.savedUncredited }),
  }
})
vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      agentCatalog: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'kirocrew' })),
      workspaces: vi.fn(() => Promise.resolve({ workspaces: [{ name: 'default' }] })),
      availableModels: vi.fn(() => Promise.resolve({ models: [] })),
      members: vi.fn(() => Promise.resolve({ members: [] })),
      createKirocrewAgent: vi.fn(() => Promise.resolve({ ok: true, name: 'Scout' })),
    },
  }
})

const props = { onClose: vi.fn(), onCreated: vi.fn(), existingNames: [] as string[] }
const create = async (guided: boolean) => {
  renderWithProviders(<NewCrewmateDialog open embedded guided={guided} initialDraft={{ name: 'Scout', goal: '' }} {...props} />)
  await screen.findByTestId('crewmate-create-form')
  fireEvent.click(screen.getByTestId('crewmate-create-submit'))
  await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
}

describe('NewCrewmateDialog guided door', () => {
  beforeEach(() => {
    guide.headers = undefined
    guide.sync.mockClear(); guide.refused.mockClear(); guide.savedUncredited.mockClear()
    vi.mocked(api.createKirocrewAgent).mockReset()
    vi.mocked(api.createKirocrewAgent).mockResolvedValue({ ok: true, name: 'Scout' })
    props.onCreated = vi.fn()
  })

  it('a guided create waits for the guide, then carries its headers on the create alone', async () => {
    guide.headers = { 'X-Guide-Id': 'g1', 'X-Guide-Tab': 't1', 'X-Guide-Revision': '3' }
    await create(true)
    expect(guide.sync).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.createKirocrewAgent).mock.calls[0][1]).toEqual(guide.headers)
    await waitFor(() => expect(props.onCreated).toHaveBeenCalled(), { timeout: 4000 })
    expect(guide.savedUncredited).not.toHaveBeenCalled()
  })

  it('a guided create the guide had not reached yet closes the guide honestly', async () => {
    await create(true)
    expect(vi.mocked(api.createKirocrewAgent).mock.calls[0]).toHaveLength(1)
    await waitFor(() => expect(guide.savedUncredited).toHaveBeenCalledTimes(1), { timeout: 4000 })
  })

  it('a refused guided create stops the guide waiting', async () => {
    guide.headers = { 'X-Guide-Id': 'g1' }
    vi.mocked(api.createKirocrewAgent).mockRejectedValue(new ApiError(409, 'exists', '{"code":"agent_exists"}'))
    await create(true)
    await waitFor(() => expect(guide.refused).toHaveBeenCalledTimes(1))
    expect(props.onCreated).not.toHaveBeenCalled()
  })

  it('an unguided door never touches the guide', async () => {
    guide.headers = { 'X-Guide-Id': 'g1' }
    await create(false)
    expect(guide.sync).not.toHaveBeenCalled()
    expect(vi.mocked(api.createKirocrewAgent).mock.calls[0]).toHaveLength(1)
    await waitFor(() => expect(props.onCreated).toHaveBeenCalled(), { timeout: 4000 })
    expect(guide.savedUncredited).not.toHaveBeenCalled()
  })
})
