/**
 * Settings > Agent Harness > Strands: provider, key and model.
 *
 * The claims under test are the card's write contract: a typed value goes to
 * the vault and only its `secret://` reference to `agent.strands_env`; the vault
 * is written before the reference and the reference removed before the vault
 * entry; a stored value is never shown; the provider and model are plain config.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../../api/client'
import { StrandsSetupCard } from './StrandsSetupCard'
import { readStrandsModel, readStrandsProvider, STRANDS_PROVIDERS } from './strandsProviders'

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <StrandsSetupCard />
    </QueryClientProvider>,
  )
}

/** One mutable config, so each re-read sees the write before it. */
let config: { agent: Record<string, unknown> }
let vault: string[]
const calls: string[] = []

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
    vault = vault.filter(n => n !== name)
    return {} as never
  })
  vi.spyOn(api, 'patchConfig').mockImplementation(async (path: string, value: unknown) => {
    calls.push(`patch ${path}`)
    config.agent[path.replace('agent.', '')] = value
    return {} as never
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('StrandsSetupCard', () => {
  it('lists every Strands provider, Bedrock first', async () => {
    renderCard()
    const trigger = await screen.findByRole('combobox', { name: 'Provider' })
    expect(trigger.textContent).toContain('Amazon Bedrock')
    expect(STRANDS_PROVIDERS.map(p => p.id)).toEqual([
      'bedrock', 'bedrock-mantle', 'anthropic', 'openai', 'google', 'ollama', 'litellm',
    ])
  })

  it('stores a key in the vault, then maps only its reference', async () => {
    config.agent.strands_provider = 'anthropic'
    renderCard()
    const field = await screen.findByLabelText('ANTHROPIC_API_KEY')
    fireEvent.change(field, { target: { value: 'sk-ant-not-real' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls).toEqual(['vault+STRANDS_ANTHROPIC_API_KEY', 'patch agent.strands_env']))
    expect(api.secretsSave).toHaveBeenCalledWith('STRANDS_ANTHROPIC_API_KEY', 'sk-ant-not-real')
    expect(config.agent.strands_env).toEqual({ ANTHROPIC_API_KEY: 'secret://STRANDS_ANTHROPIC_API_KEY' })
    // Write-only: the saved state shows a mask and a Replace action, never the value.
    await screen.findByRole('button', { name: /Replace/ })
    expect(document.body.textContent).not.toContain('sk-ant-not-real')
  })

  it('takes back the vault entry it created when the config refuses the reference', async () => {
    config.agent.strands_provider = 'anthropic'
    vi.mocked(api.patchConfig).mockRejectedValueOnce(new Error('400'))
    renderCard()
    fireEvent.change(await screen.findByLabelText('ANTHROPIC_API_KEY'), { target: { value: 'sk-x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByText('Could not save this value. Try again.')
    expect(api.secretsDelete).toHaveBeenCalledWith('STRANDS_ANTHROPIC_API_KEY')
    expect(vault).toEqual([])
  })

  it('runs overlapping saves one after another, so neither entry is lost', async () => {
    // Hold the first PATCH open: without the card's queue the second save would read
    // the map before the first lands and write it back without the first entry.
    let release: () => void = () => undefined
    const patch = vi.mocked(api.patchConfig)
    const real = patch.getMockImplementation()!
    patch.mockImplementationOnce(
      (path, value) => new Promise(resolve => { release = () => resolve(real(path, value)) }) as never,
    )
    renderCard()
    fireEvent.change(await screen.findByLabelText('AWS_REGION (optional)'), { target: { value: 'us-west-2' } })
    fireEvent.change(screen.getByLabelText('AWS_PROFILE (optional)'), { target: { value: 'dev' } })
    const regionSave = await within(screen.getByTestId('strands-field-AWS_REGION')).findByRole('button', { name: 'Save' })
    const profileSave = await within(screen.getByTestId('strands-field-AWS_PROFILE')).findByRole('button', { name: 'Save' })
    fireEvent.click(regionSave)
    fireEvent.click(profileSave)
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1))
    // The second save has not started: only the first field's vault write ran.
    expect(calls).toEqual(['vault+STRANDS_AWS_REGION'])
    release()
    await waitFor(() =>
      expect(config.agent.strands_env).toEqual({
        AWS_REGION: 'secret://STRANDS_AWS_REGION',
        AWS_PROFILE: 'secret://STRANDS_AWS_PROFILE',
      }),
    )
  })

  it('clears the old provider’s model before it switches provider', async () => {
    config.agent.strands_provider = 'openai'
    config.agent.strands_model = 'openai/gpt-x'
    renderCard()
    const trigger = await screen.findByRole('combobox', { name: 'Provider' })
    await waitFor(() => expect(trigger.textContent).toContain('OpenAI'))
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'Ollama' }))
    await waitFor(() =>
      expect(calls).toEqual(['patch agent.strands_model', 'patch agent.strands_provider']),
    )
    expect(config.agent.strands_model).toBe('auto')
    expect(config.agent.strands_provider).toBe('ollama')
  })

  it('replaces a value the map already names with a vault write alone', async () => {
    config.agent.strands_provider = 'openai'
    config.agent.strands_env = { OPENAI_API_KEY: 'secret://STRANDS_OPENAI_API_KEY' }
    vault = ['STRANDS_OPENAI_API_KEY']
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: /Replace/ }))
    fireEvent.change(screen.getByLabelText('OPENAI_API_KEY'), { target: { value: 'sk-new' } })
    fireEvent.click(within(screen.getByTestId('strands-field-OPENAI_API_KEY')).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls).toEqual(['vault+STRANDS_OPENAI_API_KEY']))
    expect(api.patchConfig).not.toHaveBeenCalled()
  })

  it('keeps the vault entry when the refused-looking PATCH had in fact landed', async () => {
    // The response was lost after the server committed: a re-read shows the reference.
    config.agent.strands_provider = 'anthropic'
    const patch = vi.mocked(api.patchConfig)
    const real = patch.getMockImplementation()!
    patch.mockImplementationOnce(async (path, value) => {
      await real(path, value)
      throw new Error('connection reset')
    })
    renderCard()
    fireEvent.change(await screen.findByLabelText('ANTHROPIC_API_KEY'), { target: { value: 'sk-x' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }))
    await screen.findByText('Could not save this value. Try again.')
    expect(api.secretsDelete).not.toHaveBeenCalled()
    expect(vault).toEqual(['STRANDS_ANTHROPIC_API_KEY'])
  })

  it('keeps the other entries of the map when it adds one', async () => {
    config.agent.strands_provider = 'openai'
    config.agent.strands_env = { AWS_REGION: 'secret://my-region' }
    vault = ['my-region']
    renderCard()
    fireEvent.change(await screen.findByLabelText('OPENAI_API_KEY'), { target: { value: 'sk-x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(config.agent.strands_env).toEqual({
        AWS_REGION: 'secret://my-region',
        OPENAI_API_KEY: 'secret://STRANDS_OPENAI_API_KEY',
      }),
    )
  })

  it('removes the reference before the vault entry, after asking', async () => {
    config.agent.strands_provider = 'google'
    config.agent.strands_env = { GEMINI_API_KEY: 'secret://STRANDS_GEMINI_API_KEY' }
    vault = ['STRANDS_GEMINI_API_KEY']
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    expect(calls).toEqual([])
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(calls).toEqual(['patch agent.strands_env', 'vault-STRANDS_GEMINI_API_KEY']))
    expect(config.agent.strands_env).toEqual({})
  })

  it('unmaps a person’s own vault entry without deleting it', async () => {
    config.agent.strands_provider = 'google'
    config.agent.strands_env = { GEMINI_API_KEY: 'secret://my-gemini' }
    vault = ['my-gemini']
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(calls).toEqual(['patch agent.strands_env']))
    expect(vault).toEqual(['my-gemini'])
  })

  it('asks again for a value whose reference names a missing vault entry', async () => {
    config.agent.strands_provider = 'anthropic'
    config.agent.strands_env = { ANTHROPIC_API_KEY: 'secret://STRANDS_ANTHROPIC_API_KEY' }
    renderCard()
    expect(await screen.findByLabelText('ANTHROPIC_API_KEY')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Replace/ })).not.toBeInTheDocument()
  })

  it('shows Bedrock as needing no key: every AWS field is optional', async () => {
    renderCard()
    await screen.findByTestId('strands-field-AWS_REGION')
    expect(screen.getByLabelText('AWS_BEARER_TOKEN_BEDROCK (optional)')).toBeInTheDocument()
    expect(screen.getByLabelText('AWS_REGION (optional)')).toBeInTheDocument()
    expect(screen.getByLabelText('AWS_PROFILE (optional)')).toBeInTheDocument()
  })

  it('writes the model as typed, and auto when cleared', async () => {
    config.agent.strands_model = 'some-model'
    renderCard()
    const input = await screen.findByLabelText('Model')
    await waitFor(() => expect((input as HTMLInputElement).value).toBe('some-model'))
    fireEvent.change(input, { target: { value: '' } })
    const modelRow = input.closest('[data-setting-label]') as HTMLElement
    fireEvent.click(within(modelRow).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.strands_model', 'auto'))
  })

  it('reads an unset or empty provider as Bedrock and auto as no model', () => {
    expect(readStrandsProvider({ agent: { strands_provider: '' } }).id).toBe('bedrock')
    expect(readStrandsProvider({ agent: {} }).id).toBe('bedrock')
    expect(readStrandsProvider({ agent: { strands_provider: 'OpenAI' } }).id).toBe('openai')
    expect(readStrandsModel({ agent: { strands_model: 'auto' } })).toBe('')
    expect(readStrandsModel({ agent: { strands_model: ' gpt-x ' } })).toBe('gpt-x')
  })

  it('writes a picked provider by its id, Bedrock included', async () => {
    config.agent.strands_provider = 'openai'
    renderCard()
    const trigger = await screen.findByRole('combobox', { name: 'Provider' })
    await waitFor(() => expect(trigger.textContent).toContain('OpenAI'))
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'Amazon Bedrock' }))
    await waitFor(() => expect(config.agent.strands_provider).toBe('bedrock'))
  })
})
