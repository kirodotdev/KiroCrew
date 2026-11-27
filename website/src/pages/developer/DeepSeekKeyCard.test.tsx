/**
 * Settings > Agent Harness > DeepSeek Harness > Provider key.
 *
 * The claims: a typed key goes to the vault under DEEPSEEK_HARNESS_API_KEY first,
 * and only its `secret://` reference to `agent.deepseek_env` second; the saved key
 * is never shown; Remove unmaps DEEPSEEK_API_KEY only and deletes nothing from the
 * vault; a reference to a vault entry that is gone shows the missing state.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../../api/client'
import { DeepSeekKeyCard } from './DeepSeekKeyCard'

let config: { agent: Record<string, unknown> }
let vault: string[]
const calls: string[] = []

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <DeepSeekKeyCard />
    </QueryClientProvider>,
  )
}

const deepseekEnv = () => config.agent.deepseek_env as Record<string, string> | undefined

beforeEach(() => {
  config = { agent: {} }
  vault = []
  calls.length = 0
  vi.spyOn(api, 'kirocrewConfig').mockImplementation(async () => structuredClone(config) as never)
  vi.spyOn(api, 'secretsList').mockImplementation(async () => ({ names: [...vault], managed: [] }) as never)
  vi.spyOn(api, 'secretsSave').mockImplementation(async (name: string) => {
    calls.push(`vault+${name}`)
    if (!vault.includes(name)) vault.push(name)
    return {} as never
  })
  vi.spyOn(api, 'secretsDelete').mockImplementation(async (name: string) => {
    calls.push(`vault-${name}`)
    return {} as never
  })
  vi.spyOn(api, 'patchConfig').mockImplementation(async (path: string, value: unknown) => {
    calls.push(`patch ${path}`)
    // The gateway's keyed merge.
    const merged = { ...(deepseekEnv() ?? {}) }
    for (const [k, v] of Object.entries(value as Record<string, string | null>)) {
      if (v === null) delete merged[k]
      else merged[k] = v
    }
    config.agent.deepseek_env = merged
    return {} as never
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('DeepSeekKeyCard', () => {
  it('saves the key to the vault first, then maps only its reference', async () => {
    renderCard()
    expect(await screen.findByRole('region', { name: 'Provider key' })).toBeInTheDocument()
    fireEvent.change(await screen.findByLabelText('DEEPSEEK_API_KEY'), { target: { value: 'sk-not-real' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls).toEqual(['vault+DEEPSEEK_HARNESS_API_KEY', 'patch agent.deepseek_env']))
    expect(api.secretsSave).toHaveBeenCalledWith('DEEPSEEK_HARNESS_API_KEY', 'sk-not-real')
    expect(api.patchConfig).toHaveBeenCalledWith('agent.deepseek_env', {
      DEEPSEEK_API_KEY: 'secret://DEEPSEEK_HARNESS_API_KEY',
    })
    // Write-only: the saved state shows a mask and Replace, never the key.
    await screen.findByRole('button', { name: /Replace/ })
    expect(document.body.textContent).not.toContain('sk-not-real')
  })

  it('keeps another provider route entry of the map when it saves', async () => {
    config.agent.deepseek_env = { OPENROUTER_API_KEY: 'secret://or' }
    vault = ['or']
    renderCard()
    fireEvent.change(await screen.findByLabelText('DEEPSEEK_API_KEY'), { target: { value: 'sk-x' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(deepseekEnv()).toEqual({
        OPENROUTER_API_KEY: 'secret://or',
        DEEPSEEK_API_KEY: 'secret://DEEPSEEK_HARNESS_API_KEY',
      }),
    )
  })

  it('Remove asks first, then unmaps the key and deletes nothing from the vault', async () => {
    config.agent.deepseek_env = { DEEPSEEK_API_KEY: 'secret://DEEPSEEK_HARNESS_API_KEY' }
    vault = ['DEEPSEEK_HARNESS_API_KEY']
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    expect(calls).toEqual([])
    const field = within(screen.getByTestId('deepseek-field-DEEPSEEK_API_KEY'))
    fireEvent.click(field.getAllByRole('button', { name: 'Remove' }).at(-1)!)
    await waitFor(() => expect(calls).toEqual(['patch agent.deepseek_env']))
    expect(api.patchConfig).toHaveBeenCalledWith('agent.deepseek_env', { DEEPSEEK_API_KEY: null })
    expect(deepseekEnv()).toEqual({})
    expect(api.secretsDelete).not.toHaveBeenCalled()
    expect(vault).toEqual(['DEEPSEEK_HARNESS_API_KEY'])
  })

  it('shows the missing state, and offers Remove, for a reference whose vault entry is gone', async () => {
    config.agent.deepseek_env = { DEEPSEEK_API_KEY: 'secret://DEEPSEEK_HARNESS_API_KEY' }
    renderCard()
    const row = await screen.findByTestId('deepseek-dangling-DEEPSEEK_API_KEY')
    expect(row.textContent).toContain('no longer in Secrets')
    expect(row.textContent).toContain('DeepSeek Harness')
    expect(screen.queryByRole('button', { name: /Replace/ })).not.toBeInTheDocument()
    fireEvent.click(within(row).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(deepseekEnv()).toEqual({}))
    expect(api.secretsDelete).not.toHaveBeenCalled()
  })

  it('shows a field reference to a stored entry as saved, as the gateway resolves it', async () => {
    config.agent.deepseek_env = { DEEPSEEK_API_KEY: 'secret://provider-key#value' }
    vault = ['provider-key']
    renderCard()
    await screen.findByRole('button', { name: /Replace/ })
    const field = within(screen.getByTestId('deepseek-field-DEEPSEEK_API_KEY'))
    expect(field.getByRole('button', { name: 'Remove' })).toBeInTheDocument()
    expect(field.getByText('••••••••')).toBeInTheDocument()
    expect(screen.queryByTestId('deepseek-dangling-DEEPSEEK_API_KEY')).not.toBeInTheDocument()
  })

  it('names DeepSeek Harness, not Strands, when the settings cannot be read', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('boom'))
    renderCard()
    const notice = await screen.findByText(/Could not read your DeepSeek Harness settings/)
    expect(notice).toBeInTheDocument()
  })
})
