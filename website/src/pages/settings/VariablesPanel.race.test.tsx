import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { VariablesPanel } from './VariablesPanel'
import { api, type VariablesView } from '../../api/client'

vi.mock('../../api/client', () => ({ api: { variables: vi.fn(), saveVariables: vi.fn() } }))

const variables = vi.mocked(api.variables!)
const saveVariables = vi.mocked(api.saveVariables!)

function view(over: Partial<VariablesView> = {}): VariablesView {
  return { global: {}, workspaces: {}, effective: {}, winning_scope: {}, ...over }
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <VariablesPanel />
    </QueryClientProvider>,
  )
}

const addButton = () => screen.getAllByRole('button', { name: 'Add' })[0]
const ready = () => waitFor(() => expect(addButton()).toBeEnabled())
/** Only the add form's fields are named plain 'Name'/'Value'; a row's value input is
 *  named '<NAME> Value'. */
const field = (name: 'Name' | 'Value') => screen.getByRole('textbox', { name })

/** Fill the add form and submit, which is the only interaction that issues a save. */
function addPair(name: string, value: string) {
  fireEvent.change(field('Name'), { target: { value: name } })
  fireEvent.change(field('Value'), { target: { value } })
  fireEvent.click(addButton())
}

/**
 * A save is a per-key patch, so it cannot disturb a key it does not name — but the
 * pairs the panel is HOLDING still drive what it offers, and a stale table can send
 * a set for a key whose stored value has moved on. If the controls re-enable before
 * the post-save refetch lands, a second edit is composed against pre-save state.
 *
 * The guard is that `onSuccess` RETURNS the invalidation, keeping the mutation
 * pending — and `busy`, which disables every control, derives from `isPending`.
 * These tests pin that observable consequence, not the presence of a keyword.
 */
describe('VariablesPanel save/refetch ordering', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('keeps the controls disabled until the post-save refetch settles', async () => {
    let releaseRefetch: (v: VariablesView) => void = () => {}
    let call = 0
    variables.mockImplementation(() => {
      call += 1
      // The first read resolves at once; the post-save refetch is held open so the
      // window between "save returned" and "fresh pairs arrived" is observable.
      if (call === 1) return Promise.resolve(view({ global: { A: '1' } }))
      return new Promise<VariablesView>(resolve => {
        releaseRefetch = resolve
      })
    })
    saveVariables.mockResolvedValue({ ok: true })

    mount()
    await ready()
    addPair('REGION', 'eu-west-1')

    await waitFor(() => expect(saveVariables).toHaveBeenCalledTimes(1))
    // Save resolved, refetch has NOT. This is exactly the window in which a second
    // save would read stale pairs and overwrite the first, so it must stay shut.
    await waitFor(() => expect(addButton()).toBeDisabled())

    releaseRefetch(view({ global: { A: '1', REGION: 'eu-west-1' } }))
    await ready()
  })

  it('does not strand the panel disabled when the refetch fails', async () => {
    // The flip side of holding `busy` through the refetch: a rejected refetch must
    // still settle the mutation, or the panel is left disabled with no way back.
    let call = 0
    variables.mockImplementation(() => {
      call += 1
      if (call === 1) return Promise.resolve(view({ global: { A: '1' } }))
      return Promise.reject(new Error('refetch failed'))
    })
    saveVariables.mockResolvedValue({ ok: true })

    mount()
    await ready()
    addPair('REGION', 'eu-west-1')

    await waitFor(() => expect(saveVariables).toHaveBeenCalledTimes(1))
    await ready()
  })
})
