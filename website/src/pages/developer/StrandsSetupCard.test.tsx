/**
 * Settings > Agent Harness > Strands: provider, key and model.
 *
 * The claims under test are the card's write contract: a typed value goes to
 * the vault and only its `secret://` reference to `agent.strands_env`; the vault
 * is written before the reference and the reference removed before the vault
 * entry; a stored value is never shown; the provider and model are plain config.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
    if (path === 'agent.strands_env') {
      // The gateway's keyed merge: set each named key, drop each null one.
      const merged = { ...((config.agent.strands_env as Record<string, string>) ?? {}) }
      for (const [k, v] of Object.entries(value as Record<string, string | null>)) {
        if (v === null) delete merged[k]
        else merged[k] = v
      }
      config.agent.strands_env = merged
    } else {
      config.agent[path.replace('agent.', '')] = value
    }
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
    fireEvent.change(await screen.findByLabelText('AWS_REGION'), { target: { value: 'us-west-2' } })
    fireEvent.change(screen.getByLabelText('AWS_BEARER_TOKEN_BEDROCK'), { target: { value: 'tok' } })
    const regionSave = await within(screen.getByTestId('strands-field-AWS_REGION')).findByRole('button', { name: 'Save' })
    const profileSave = await within(screen.getByTestId('strands-field-AWS_BEARER_TOKEN_BEDROCK')).findByRole('button', { name: 'Save' })
    fireEvent.click(regionSave)
    fireEvent.click(profileSave)
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1))
    // The second save has not started: only the first field's vault write ran.
    expect(calls).toEqual(['vault+STRANDS_AWS_REGION'])
    release()
    await waitFor(() =>
      expect(config.agent.strands_env).toEqual({
        AWS_REGION: 'secret://STRANDS_AWS_REGION',
        AWS_BEARER_TOKEN_BEDROCK: 'secret://STRANDS_AWS_BEARER_TOKEN_BEDROCK',
      }),
    )
  })

  it('switches provider with one write and keeps the saved model', async () => {
    // The backend runs the model under the provider picked here, so a stale id is
    // never sent to the old provider; one write cannot half-land.
    config.agent.strands_provider = 'openai'
    config.agent.strands_model = 'gpt-x'
    renderCard()
    const trigger = await screen.findByRole('combobox', { name: 'Provider' })
    await waitFor(() => expect(trigger.textContent).toContain('OpenAI'))
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'Ollama' }))
    await waitFor(() => expect(calls).toEqual(['patch agent.strands_provider']))
    expect(config.agent.strands_model).toBe('gpt-x')
    expect(config.agent.strands_provider).toBe('ollama')
  })

  it('keeps the provider picker focusable and refuses a pick while its own write runs', async () => {
    // A native `disabled` takes the pressed picker out of the tab order, so a
    // keyboard user lands on the page body while the write runs. aria-disabled
    // plus a guard keep focus and still refuse a second pick.
    let release: () => void = () => undefined
    const patch = vi.mocked(api.patchConfig)
    patch.mockImplementationOnce(
      (path, value) => new Promise(resolve => {
        release = () => resolve(undefined as never)
        void value
        void path
      }) as never,
    )
    config.agent.strands_provider = 'openai'
    renderCard()
    const picker = await screen.findByRole('combobox', { name: 'Provider' })
    await waitFor(() => expect(picker.textContent).toContain('OpenAI'))
    fireEvent.click(picker)
    fireEvent.click(await screen.findByRole('option', { name: 'Ollama' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('agent.strands_provider', 'ollama'))
    // While the write runs the picker stays in the tab order, not native disabled.
    expect(picker).not.toBeDisabled()
    expect(picker).toHaveAttribute('aria-disabled', 'true')
    expect(picker).toHaveAttribute('aria-busy', 'true')
    // A second pick is ignored, so only the first write ran.
    fireEvent.click(picker)
    const anthropic = screen.queryByRole('option', { name: 'Anthropic' })
    if (anthropic) fireEvent.click(anthropic)
    expect(patch.mock.calls.filter(c => c[0] === 'agent.strands_provider')).toHaveLength(1)
    release()
    await waitFor(() => expect(picker).not.toHaveAttribute('aria-disabled'))
    expect(picker).not.toHaveAttribute('aria-busy')
  })

  it('locks the provider picker while a field save runs', async () => {
    // Switching mid-save would unmount the field and drop the save's outcome, so a
    // failed key would vanish with no notice.
    let release: () => void = () => undefined
    vi.mocked(api.secretsSave).mockImplementationOnce(
      () => new Promise(resolve => { release = () => resolve({} as never) }) as never,
    )
    config.agent.strands_provider = 'anthropic'
    renderCard()
    const picker = await screen.findByRole('combobox', { name: 'Provider' })
    fireEvent.change(await screen.findByLabelText('ANTHROPIC_API_KEY'), { target: { value: 'sk-ant-not-real' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.secretsSave).toHaveBeenCalled())
    expect(picker).toBeDisabled()
    release()
    await waitFor(() => expect(picker).not.toBeDisabled())
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

  it('leaves the vault entry in place when the reference write fails', async () => {
    // No rollback delete: one could take out an entry another tab had just mapped.
    config.agent.strands_provider = 'anthropic'
    vi.mocked(api.patchConfig).mockRejectedValueOnce(new Error('400'))
    renderCard()
    fireEvent.change(await screen.findByLabelText('ANTHROPIC_API_KEY'), { target: { value: 'sk-x' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }))
    await screen.findByText('Could not save this value. Try again.')
    expect(api.secretsDelete).not.toHaveBeenCalled()
    expect(vault).toEqual(['STRANDS_ANTHROPIC_API_KEY'])
  })

  it('drops a model draft when the provider switch starts', async () => {
    config.agent.strands_provider = 'openai'
    renderCard()
    const input = (await screen.findByLabelText('Model')) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'gpt-x' } })
    const trigger = screen.getByRole('combobox', { name: 'Provider' })
    await waitFor(() => expect(trigger.textContent).toContain('OpenAI'))
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'Ollama' }))
    await waitFor(() => expect(config.agent.strands_provider).toBe('ollama'))
    expect(input.value).toBe('')
    expect(api.patchConfig).not.toHaveBeenCalledWith('agent.strands_model', 'gpt-x')
  })

  it('keeps a correction typed while the save was running', async () => {
    config.agent.strands_provider = 'openai'
    let release: () => void = () => undefined
    const save = vi.mocked(api.secretsSave)
    const real = save.getMockImplementation()!
    save.mockImplementationOnce(
      (name, value) => new Promise(resolve => { release = () => resolve(real(name, value)) }) as never,
    )
    renderCard()
    const field = await screen.findByLabelText('OPENAI_API_KEY')
    fireEvent.change(field, { target: { value: 'sk-first' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }))
    await waitFor(() => expect(save).toHaveBeenCalled())
    fireEvent.change(field, { target: { value: 'sk-second' } })
    release()
    await waitFor(() => expect(config.agent.strands_env).toEqual({ OPENAI_API_KEY: 'secret://STRANDS_OPENAI_API_KEY' }))
    await waitFor(() => expect(api.secretsList).toHaveBeenCalledTimes(2))
    expect((screen.getByLabelText('OPENAI_API_KEY') as HTMLInputElement).value).toBe('sk-second')
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

  it('Remove asks first, then unmaps only its own key and deletes nothing from the vault', async () => {
    config.agent.strands_provider = 'google'
    config.agent.strands_env = { GEMINI_API_KEY: 'secret://STRANDS_GEMINI_API_KEY', AWS_REGION: 'secret://r' }
    vault = ['STRANDS_GEMINI_API_KEY', 'r']
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    expect(calls).toEqual([])
    fireEvent.click(within(screen.getByTestId('strands-field-GEMINI_API_KEY')).getAllByRole('button', { name: 'Remove' }).at(-1)!)
    await waitFor(() => expect(calls).toEqual(['patch agent.strands_env']))
    expect(api.patchConfig).toHaveBeenCalledWith('agent.strands_env', { GEMINI_API_KEY: null })
    expect(config.agent.strands_env).toEqual({ AWS_REGION: 'secret://r' })
    expect(api.secretsDelete).not.toHaveBeenCalled()
    expect(vault).toEqual(['STRANDS_GEMINI_API_KEY', 'r'])
  })

  it('keeps keyboard focus on the remove confirm: Cancel on arm, Remove after Cancel', async () => {
    // Arming swaps the focused Remove for the confirm row, and Cancel swaps it
    // back; each swap unmounts the focused button, so focus would fall to the
    // page. Arming lands on Cancel, the safe choice, as Settings > Secrets does.
    config.agent.strands_provider = 'google'
    config.agent.strands_env = { GEMINI_API_KEY: 'secret://STRANDS_GEMINI_API_KEY' }
    vault = ['STRANDS_GEMINI_API_KEY']
    renderCard()
    const field = () => within(screen.getByTestId('strands-field-GEMINI_API_KEY'))
    const remove = await screen.findByRole('button', { name: 'Remove' })
    remove.focus()
    fireEvent.click(remove)
    await waitFor(() => expect(document.activeElement).toBe(field().getByRole('button', { name: 'Cancel' })))
    fireEvent.click(field().getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(document.activeElement).toBe(field().getByRole('button', { name: 'Remove' })))
    expect(calls).toEqual([])
  })

  it('keeps keyboard focus on a key Save while it runs and after a refusal', async () => {
    config.agent.strands_provider = 'anthropic'
    let refuse: (reason: Error) => void = () => {}
    vi.mocked(api.secretsSave).mockImplementationOnce(
      () => new Promise((_resolve, reject) => { refuse = reject }) as never,
    )
    renderCard()
    const input = await screen.findByLabelText('ANTHROPIC_API_KEY')
    fireEvent.change(input, { target: { value: 'sk-ant-not-real' } })
    const field = within(screen.getByTestId('strands-field-ANTHROPIC_API_KEY'))
    const save = field.getByRole('button', { name: 'Save' })
    save.focus()
    fireEvent.click(save)
    await waitFor(() => expect(save).toHaveAttribute('aria-disabled', 'true'))
    expect(save).not.toBeDisabled()
    expect(save).toHaveFocus()

    fireEvent.click(save)
    expect(api.secretsSave).toHaveBeenCalledTimes(1)

    await act(async () => { refuse(new Error('refused')) })
    await waitFor(() => expect(save).not.toHaveAttribute('aria-disabled'))
    expect(save).toHaveFocus()
  })

  it('moves focus to the saved field, not the page, when a key Save lands', async () => {
    config.agent.strands_provider = 'anthropic'
    renderCard()
    const input = await screen.findByLabelText('ANTHROPIC_API_KEY')
    fireEvent.change(input, { target: { value: 'sk-ant-not-real' } })
    const field = () => within(screen.getByTestId('strands-field-ANTHROPIC_API_KEY'))
    const save = field().getByRole('button', { name: 'Save' })
    save.focus()
    fireEvent.click(save)
    const replace = await field().findByRole('button', { name: /Replace/ })
    await waitFor(() => expect(replace).toHaveFocus())
  })

  it('keeps the landed key Save enabled and focused until Replace shows', async () => {
    // The save clears the draft before the re-read lands; a natively disabled
    // Save in that gap would drop focus to the page.
    config.agent.strands_provider = 'anthropic'
    renderCard()
    const input = await screen.findByLabelText('ANTHROPIC_API_KEY')
    fireEvent.change(input, { target: { value: 'sk-ant-not-real' } })
    const field = () => within(screen.getByTestId('strands-field-ANTHROPIC_API_KEY'))
    const save = field().getByRole('button', { name: 'Save' })
    let reread: () => void = () => {}
    const read = vi.mocked(api.kirocrewConfig).getMockImplementation()!
    vi.mocked(api.secretsSave).mockImplementationOnce(async (name: string) => {
      vault.push(name)
      vi.mocked(api.kirocrewConfig).mockImplementation(
        () => new Promise(resolve => { reread = () => resolve(read() as never) }) as never,
      )
      return {} as never
    })
    save.focus()
    fireEvent.click(save)
    await waitFor(() => expect((input as HTMLInputElement).value).toBe(''))
    expect(save).not.toBeDisabled()
    expect(save).toHaveFocus()
    vi.mocked(api.kirocrewConfig).mockImplementation(read)
    await act(async () => { reread() })
    const replace = await field().findByRole('button', { name: /Replace/ })
    await waitFor(() => expect(replace).toHaveFocus())
  })

  it('keeps focus on the confirm Remove when the remove is refused', async () => {
    config.agent.strands_provider = 'google'
    config.agent.strands_env = { GEMINI_API_KEY: 'secret://STRANDS_GEMINI_API_KEY' }
    vault = ['STRANDS_GEMINI_API_KEY']
    vi.mocked(api.patchConfig).mockRejectedValueOnce(new Error('refused'))
    renderCard()
    const field = () => within(screen.getByTestId('strands-field-GEMINI_API_KEY'))
    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    const del = field().getAllByRole('button', { name: 'Remove' }).at(-1)!
    del.focus()
    fireEvent.click(del)
    expect(await screen.findByText('refused', { exact: false })).toBeInTheDocument()
    await waitFor(() => expect(del).not.toHaveAttribute('aria-disabled'))
    expect(del).toBeInTheDocument()
    expect(del).toHaveFocus()
  })

  it('returns focus to the dangling Remove when its remove is refused', async () => {
    config.agent.strands_provider = 'google'
    config.agent.strands_env = { GEMINI_API_KEY: 'secret://STRANDS_GEMINI_API_KEY' }
    vault = []
    let refuse: (reason: Error) => void = () => {}
    vi.mocked(api.patchConfig).mockImplementationOnce(
      () => new Promise((_resolve, reject) => { refuse = reject }) as never,
    )
    renderCard()
    const dangling = () => within(screen.getByTestId('strands-dangling-GEMINI_API_KEY'))
    const remove = await waitFor(() => dangling().getByRole('button', { name: 'Remove' }))
    remove.focus()
    fireEvent.click(remove)
    // The dangling row hides while its remove runs, taking the focused button.
    await waitFor(() => expect(screen.queryByTestId('strands-dangling-GEMINI_API_KEY')).toBeNull())
    await act(async () => { refuse(new Error('refused')) })
    expect((await screen.findAllByText('refused', { exact: false })).length).toBeGreaterThan(0)
    await waitFor(() => expect(dangling().getByRole('button', { name: 'Remove' })).toHaveFocus())
  })

  it('leaves focus on another field the user moved to while a key save ran', async () => {
    config.agent.strands_provider = 'anthropic'
    let land: () => void = () => {}
    const save0 = vi.mocked(api.secretsSave).getMockImplementation()!
    vi.mocked(api.secretsSave).mockImplementationOnce(
      (name: string, value: string) => new Promise(resolve => { land = () => resolve(save0(name, value)) }) as never,
    )
    renderCard()
    const input = await screen.findByLabelText('ANTHROPIC_API_KEY')
    fireEvent.change(input, { target: { value: 'sk-ant-not-real' } })
    const field = () => within(screen.getByTestId('strands-field-ANTHROPIC_API_KEY'))
    const save = field().getByRole('button', { name: 'Save' })
    save.focus()
    fireEvent.click(save)
    await waitFor(() => expect(save).toHaveAttribute('aria-disabled', 'true'))
    const model = screen.getByLabelText('Model')
    model.focus()
    await act(async () => { land() })
    await field().findByRole('button', { name: /Replace/ })
    expect(model).toHaveFocus()
  })

  it('keeps focus on the confirm Delete while the remove runs, then moves it into the field', async () => {
    config.agent.strands_provider = 'google'
    config.agent.strands_env = { GEMINI_API_KEY: 'secret://STRANDS_GEMINI_API_KEY' }
    vault = ['STRANDS_GEMINI_API_KEY']
    let land: () => void = () => {}
    const patch = vi.mocked(api.patchConfig).getMockImplementation()!
    vi.mocked(api.patchConfig).mockImplementationOnce(
      (path: string, value: unknown) => new Promise(resolve => { land = () => resolve(patch(path, value)) }) as never,
    )
    renderCard()
    const field = () => within(screen.getByTestId('strands-field-GEMINI_API_KEY'))
    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    const del = field().getAllByRole('button', { name: 'Remove' }).at(-1)!
    del.focus()
    fireEvent.click(del)
    await waitFor(() => expect(del).toHaveAttribute('aria-disabled', 'true'))
    expect(del).not.toBeDisabled()
    expect(del).toHaveFocus()

    await act(async () => { land() })
    await waitFor(() => expect(field().getByLabelText('GEMINI_API_KEY')).toHaveFocus())
  })

  it('asks Bedrock for an API key and a region, and no profile', async () => {
    renderCard()
    await screen.findByTestId('strands-field-AWS_REGION')
    expect(screen.getByLabelText('AWS_BEARER_TOKEN_BEDROCK')).toBeInTheDocument()
    expect(screen.getByLabelText('AWS_REGION')).toBeInTheDocument()
    expect(screen.queryByTestId('strands-field-AWS_PROFILE')).toBeNull()
  })

  it('asks again for a value whose reference names a missing vault entry', async () => {
    config.agent.strands_provider = 'anthropic'
    config.agent.strands_env = { ANTHROPIC_API_KEY: 'secret://STRANDS_ANTHROPIC_API_KEY' }
    renderCard()
    expect(await screen.findByLabelText('ANTHROPIC_API_KEY')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Replace/ })).not.toBeInTheDocument()
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

  it('shows the gateway reason when a save is refused', async () => {
    // A key config.local.json pins is refused with a reason the user needs: a bare
    // "save failed" would leave them retrying a Save that cannot succeed.
    const reason = 'entry strands_model is set in config.local.json; change or remove it there'
    vi.mocked(api.patchConfig).mockRejectedValueOnce(new Error(reason))
    renderCard()
    const input = await screen.findByLabelText('Model')
    fireEvent.change(input, { target: { value: 'some-model' } })
    const modelRow = input.closest('[data-setting-label]') as HTMLElement
    fireEvent.click(within(modelRow).getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(reason, { exact: false })).toBeInTheDocument()
  })

  it('keeps keyboard focus on the model Save while it runs and after a refusal', async () => {
    let refuse: (reason: Error) => void = () => {}
    renderCard()
    const input = await screen.findByLabelText('Model')
    fireEvent.change(input, { target: { value: 'some-model' } })
    vi.mocked(api.patchConfig).mockImplementationOnce(
      () => new Promise((_resolve, reject) => { refuse = reject }) as never,
    )
    const modelRow = input.closest('[data-setting-label]') as HTMLElement
    const save = within(modelRow).getByRole('button', { name: 'Save' })
    save.focus()
    fireEvent.click(save)
    await waitFor(() => expect(save).toHaveAttribute('aria-disabled', 'true'))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.strands_model', 'some-model'))
    expect(save).not.toBeDisabled()
    expect(save).toHaveFocus()

    fireEvent.click(save)
    expect(vi.mocked(api.patchConfig).mock.calls.filter(c => c[0] === 'agent.strands_model')).toHaveLength(1)

    await act(async () => { refuse(new Error('refused')) })
    await waitFor(() => expect(save).not.toHaveAttribute('aria-disabled'))
    expect(save).toHaveFocus()
  })

  it('returns focus to the model field after a model save lands', async () => {
    renderCard()
    const input = await screen.findByLabelText('Model')
    fireEvent.change(input, { target: { value: 'some-model' } })
    const modelRow = input.closest('[data-setting-label]') as HTMLElement
    const save = within(modelRow).getByRole('button', { name: 'Save' })
    save.focus()
    fireEvent.click(save)
    await waitFor(() => expect(within(modelRow).queryByRole('button', { name: 'Save' })).toBeNull())
    await waitFor(() => expect(screen.getByLabelText('Model')).toHaveFocus())
  })

  it('leaves focus on another field the user moved to while the save settled', async () => {
    renderCard()
    const input = await screen.findByLabelText('Model')
    fireEvent.change(input, { target: { value: 'some-model' } })
    const modelRow = input.closest('[data-setting-label]') as HTMLElement
    // A key field: it stays editable while the model save runs.
    const other = [...document.querySelectorAll<HTMLInputElement>('input:not([type=hidden])')]
      .find(el => !modelRow.contains(el) && !el.disabled) as HTMLElement
    expect(other).toBeTruthy()
    // Hold the config re-read the save triggers, so the window between the
    // save landing and the field re-enabling stays open.
    let reread: () => void = () => {}
    const current = vi.mocked(api.kirocrewConfig).getMockImplementation()!
    vi.mocked(api.kirocrewConfig).mockImplementation(
      () => new Promise(resolve => { reread = () => resolve(current() as never) }) as never,
    )
    const save = within(modelRow).getByRole('button', { name: 'Save' })
    save.focus()
    fireEvent.click(save)
    await waitFor(() => expect(within(modelRow).queryByRole('button', { name: 'Save' })).toBeNull())
    other.focus()
    vi.mocked(api.kirocrewConfig).mockImplementation(current)
    await act(async () => { reread() })
    await waitFor(() => expect(screen.getByLabelText('Model')).not.toBeDisabled())
    expect(other).toHaveFocus()
  })

  it('returns focus to the model field when Cancel drops the draft', async () => {
    renderCard()
    const input = await screen.findByLabelText('Model')
    fireEvent.change(input, { target: { value: 'some-model' } })
    const modelRow = input.closest('[data-setting-label]') as HTMLElement
    const cancel = within(modelRow).getByRole('button', { name: 'Cancel' })
    cancel.focus()
    fireEvent.click(cancel)
    await waitFor(() => expect(within(modelRow).queryByRole('button', { name: 'Cancel' })).toBeNull())
    expect(screen.getByLabelText('Model')).toHaveFocus()
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

  it('warns that a provider other than Bedrock needs a model', async () => {
    config.agent.strands_provider = 'openai'
    config.agent.strands_model = 'auto'
    renderCard()
    expect(await screen.findByTestId('strands-model-missing')).toBeInTheDocument()
  })

  it('needs no model for Bedrock', async () => {
    renderCard()
    await screen.findByLabelText('Model')
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    expect(screen.queryByTestId('strands-model-missing')).toBeNull()
  })

  it('offers Remove for a reference whose vault entry is gone', async () => {
    config.agent.strands_provider = 'ollama'
    config.agent.strands_env = { OLLAMA_HOST: 'secret://STRANDS_OLLAMA_HOST' }
    renderCard()
    const row = await screen.findByTestId('strands-dangling-OLLAMA_HOST')
    fireEvent.click(within(row).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(config.agent.strands_env).toEqual({}))
    expect(api.secretsDelete).not.toHaveBeenCalled()
  })
})
