import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SageContextValue } from '../apps/code-review-sage/context'

/**
 * The Learning rail: which namespaces exist, which ones reviews LOAD (the
 * checkbox), and which one you are reading (the row button) — three states that
 * are deliberately independent. Deleting is two-step because the patterns are
 * gone for good, and the selection has to follow the delete or the detail pane
 * keeps fetching a path that no longer exists.
 */
vi.mock('../apps/code-review-sage/api', () => ({
  sageApi: {
    namespaces: vi.fn(),
    settings: vi.fn(),
    putSettings: vi.fn(),
    createNamespace: vi.fn(),
    deleteNamespace: vi.fn(),
  },
}))

const sage: Record<string, unknown> = {}

vi.mock('../apps/code-review-sage/context', async importOriginal => {
  const actual = await importOriginal<typeof import('../apps/code-review-sage/context')>()
  return { ...actual, useSage: () => sage as unknown as SageContextValue }
})

import { sageApi } from '../apps/code-review-sage/api'
import LearningRail from '../apps/code-review-sage/components/LearningRail'

const api = sageApi as unknown as Record<string, ReturnType<typeof vi.fn>>

function mount() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  })
  return render(<QueryClientProvider client={qc}><LearningRail /></QueryClientProvider>)
}

const NAMESPACES = {
  namespaces: [
    { name: 'default', patterns: 5, candidate: 1 },
    { name: 'zzz-team', patterns: 2, candidate: 0 },
  ],
  active: ['default'],
}

beforeEach(() => {
  vi.clearAllMocks()
  Object.keys(sage).forEach(k => delete sage[k])
  Object.assign(sage, { selectedNamespace: null, selectNamespace: vi.fn() })
  api.namespaces.mockResolvedValue(NAMESPACES)
  api.settings.mockResolvedValue({
    settings: { model: null, effort: 'medium', active_namespaces: ['default'], max_concurrent: 5 },
    models: [], efforts: [], namespaces: ['default', 'zzz-team'],
  })
  api.putSettings.mockResolvedValue({})
  api.createNamespace.mockResolvedValue({})
  api.deleteNamespace.mockResolvedValue({})
})

describe('LearningRail listing', () => {
  it('lists namespaces with their pattern and pending counts', async () => {
    mount()
    expect(await screen.findByText('zzz-team')).toBeInTheDocument()
    expect(screen.getByText('default')).toBeInTheDocument()
    expect(screen.getByText(/5 patterns/)).toBeInTheDocument()
    expect(screen.getByText(/pending/)).toBeInTheDocument()
  })

  it('says it is loading, then stops', async () => {
    let release: (v: unknown) => void = () => {}
    api.namespaces.mockReturnValue(new Promise(res => { release = res }))
    mount()
    expect(screen.getByText('Loading…')).toBeInTheDocument()
    release(NAMESPACES)
    await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())
  })

  it('shows the list error', async () => {
    api.namespaces.mockRejectedValue(new Error('zzz namespaces unreadable'))
    mount()
    expect(await screen.findByText('zzz namespaces unreadable')).toBeInTheDocument()
  })

  it('marks the selected namespace and reads a different one on click', async () => {
    sage.selectedNamespace = 'default'
    mount()
    const selected = await screen.findByRole('button', { name: /Read namespace default/i })
    expect(selected).toHaveAttribute('aria-current', 'true')

    await userEvent.click(screen.getByRole('button', { name: /Read namespace zzz-team/i }))
    expect(sage.selectNamespace).toHaveBeenCalledWith('zzz-team')
  })
})

describe('LearningRail active toggles', () => {
  it('adds a namespace to the active set', async () => {
    mount()
    const box = await screen.findByRole('checkbox', { name: /Load namespace zzz-team/i })
    expect(box).not.toBeChecked()
    fireEvent.click(box)
    await waitFor(() => {
      expect(api.putSettings).toHaveBeenCalledWith({ active_namespaces: ['default', 'zzz-team'] })
    })
  })

  it('falls back to ["default"] when the last one is unticked (mirrors the backend)', async () => {
    mount()
    const box = await screen.findByRole('checkbox', { name: /Load namespace default/i })
    expect(box).toBeChecked()
    fireEvent.click(box)
    await waitFor(() => {
      expect(api.putSettings).toHaveBeenCalledWith({ active_namespaces: ['default'] })
    })
  })
})

describe('LearningRail create', () => {
  it('creates on Enter and clears the field afterwards', async () => {
    mount()
    await userEvent.click(await screen.findByRole('button', { name: /New namespace/i }))
    const field = screen.getByRole('textbox', { name: /New namespace name/i })
    await userEvent.type(field, 'zzz-new{Enter}')
    await waitFor(() => expect(api.createNamespace).toHaveBeenCalledWith('zzz-new'))
    await waitFor(() => {
      expect(screen.queryByRole('textbox', { name: /New namespace name/i })).not.toBeInTheDocument()
    })
  })

  it('creates from the add button, and ignores a blank name', async () => {
    mount()
    await userEvent.click(await screen.findByRole('button', { name: /New namespace/i }))
    const field = screen.getByRole('textbox', { name: /New namespace name/i })

    const add = screen.getByRole('button', { name: /Add namespace/i })
    expect(add).toBeDisabled()

    await userEvent.type(field, 'zzz-two')
    await userEvent.click(add)
    await waitFor(() => expect(api.createNamespace).toHaveBeenCalledWith('zzz-two'))
  })

  it('abandons the draft on Escape', async () => {
    mount()
    await userEvent.click(await screen.findByRole('button', { name: /New namespace/i }))
    const field = screen.getByRole('textbox', { name: /New namespace name/i })
    await userEvent.type(field, 'zzz-abandoned{Escape}')
    expect(screen.queryByRole('textbox', { name: /New namespace name/i })).not.toBeInTheDocument()
    expect(api.createNamespace).not.toHaveBeenCalled()
  })
})

describe('LearningRail delete', () => {
  it('never offers to delete the default namespace', async () => {
    mount()
    await screen.findByText('default')
    expect(screen.queryByRole('button', { name: /Delete namespace default/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Delete namespace zzz-team/i })).toBeInTheDocument()
  })

  it('asks first, and keeping it deletes nothing', async () => {
    mount()
    await userEvent.click(await screen.findByRole('button', { name: /Delete namespace zzz-team/i }))
    expect(screen.getByRole('button', { name: /Keep it/i })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Keep it/i }))
    expect(api.deleteNamespace).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Delete namespace zzz-team/i })).toBeInTheDocument()
  })

  it('deletes on confirm and drops a selection pointing at it', async () => {
    sage.selectedNamespace = 'zzz-team'
    mount()
    await userEvent.click(await screen.findByRole('button', { name: /Delete namespace zzz-team/i }))
    const confirmButtons = screen.getAllByRole('button', { name: /^Delete$/ })
    await userEvent.click(confirmButtons[0])
    await waitFor(() => expect(api.deleteNamespace).toHaveBeenCalledWith('zzz-team'))
    await waitFor(() => expect(sage.selectNamespace).toHaveBeenCalledWith(null))
  })

  it('surfaces a failed delete', async () => {
    api.deleteNamespace.mockRejectedValue(new Error('zzz delete refused'))
    mount()
    await userEvent.click(await screen.findByRole('button', { name: /Delete namespace zzz-team/i }))
    await userEvent.click(screen.getAllByRole('button', { name: /^Delete$/ })[0])
    expect(await screen.findByText('zzz delete refused')).toBeInTheDocument()
  })
})
