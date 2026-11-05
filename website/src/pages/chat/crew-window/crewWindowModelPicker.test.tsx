/**
 * The crew window's effort-capability read: a dropped read is ridden out before
 * the window says it could not verify the options, and a 404 (a peer slot not
 * registered yet) is not a failure.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../../../api/client', () => ({
  api: { crewPeerGet: vi.fn() },
}))

import { api } from '../../../api/client'
import { ApiError } from '../../../api/apiError'
import { useCrewWindowModelPicker } from './crewWindowModelPicker'

const peerGet = vi.mocked(api.crewPeerGet)

function renderPicker() {
  // No backoff, so the hook's own retry runs at once; nothing else retries.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const seen: boolean[] = []
  const view = renderHook(() => {
    const picker = useCrewWindowModelPicker({
      instanceId: 'peer-1',
      slotKey: 'crew-window:peer-1:s1',
      slotPath: 'api/chat/slots/s1',
      slot: { model: 'claude-opus-5-5' },
      caps: undefined,
      enabled: true,
      onWritten: vi.fn(),
    })
    seen.push(picker.effortCapsFailed)
    return picker
  }, { wrapper })
  return { ...view, seen, client }
}

const capsKey = ['crew-window', 'peer-1', 'api/chat/slots/s1', 'selection-capabilities', 'claude-opus-5-5']

describe('useCrewWindowModelPicker effort-capability read', () => {
  beforeEach(() => { peerGet.mockReset() })

  it('rides out a dropped read without flagging the effort options', async () => {
    peerGet.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    peerGet.mockResolvedValue({ known: true, effort_supported: true, effort_levels: ['low', 'high'] })
    const { result, seen } = renderPicker()

    await waitFor(() => expect(peerGet).toHaveBeenCalledTimes(2), { timeout: 1000 })
    await waitFor(() => expect(result.current.chipProps).toBeTruthy())
    expect(seen).not.toContain(true)
  })

  it('does not treat a 404 (peer slot not registered yet) as a failed read', async () => {
    peerGet.mockRejectedValue(new ApiError(404, 'slot not found'))
    const { result, client } = renderPicker()

    await waitFor(() => expect(client.getQueryState(capsKey)?.status).toBe('error'))
    expect(result.current.effortCapsFailed).toBe(false)
  })

  it('still flags a real fault', async () => {
    peerGet.mockRejectedValue(new ApiError(500, 'boom'))
    const { result } = renderPicker()

    await waitFor(() => expect(result.current.effortCapsFailed).toBe(true))
  })
})

/** Opens the picker for *slot* and renders its dropdown, as the crew window does. */
function OpenPicker({ slot, effortLevels }: { slot: { model?: string; served_model?: string }; effortLevels: string[] }) {
  const picker = useCrewWindowModelPicker({
    instanceId: 'peer-1',
    slotKey: 'crew-window:peer-1:s1',
    slotPath: 'api/chat/slots/s1',
    slot,
    caps: {
      instance_id: 'peer-1', version: '', local_version: '', version_match: true,
      agents: [], default_agent: '', models: [], effort_levels: effortLevels,
      workspaces: [], default_workspace: '', unavailable: {},
    },
    enabled: true,
    onWritten: vi.fn(),
  })
  return (
    <>
      <button type="button" onClick={e => picker.chipProps.onModelClick(e.currentTarget.getBoundingClientRect(), e.currentTarget)}>
        model
      </button>
      {picker.portal}
    </>
  )
}

function renderOpen(slot: { model?: string; served_model?: string }, effortLevels = ['low', 'high']) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  render(<QueryClientProvider client={client}><OpenPicker slot={slot} effortLevels={effortLevels} /></QueryClientProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'model' }))
  return client
}

/** The picker's selection-capability read for *model*, as its query keys it. */
function selectionStatus(client: QueryClient, model: string) {
  return client.getQueryState(['crew-window', 'peer-1', 'api/chat/slots/s1', 'selection-capabilities', model])?.status
}

describe('the crew window model picker on auto', () => {
  beforeEach(() => { peerGet.mockReset() })

  it('says effort does not apply, as the pane and composer pickers do', async () => {
    peerGet.mockResolvedValue({ known: true, effort_supported: false, effort_levels: [] })
    renderOpen({ model: '' })

    expect(await screen.findByTestId('effort-off-on-auto')).toBeTruthy()
  })

  // Both answers below say effort_supported: false, so the absence is read on
  // the settled render, not one an effort slider could replace.
  it('says nothing on a named model', async () => {
    peerGet.mockResolvedValue({ known: true, effort_supported: false, effort_levels: [] })
    const client = renderOpen({ model: 'model-x' })

    await waitFor(() => expect(selectionStatus(client, 'model-x')).toBe('success'))
    expect(screen.queryByTestId('effort-off-on-auto')).toBeNull()
  })

  it('says nothing when the peer provider takes no effort at all', async () => {
    peerGet.mockResolvedValue({ known: true, effort_supported: false, effort_levels: [] })
    const client = renderOpen({ model: '' }, [])

    await waitFor(() => expect(selectionStatus(client, '')).toBe('success'))
    expect(screen.queryByTestId('effort-off-on-auto')).toBeNull()
  })
})
