/** @vitest-environment jsdom */
/**
 * The Decision model picker's AWS-signed HTTPS option: it is offered only by a gateway
 * that reports it, writes the endpoint's parts (never an address), refuses to save
 * until the required parts are filled in, and names the field the gateway refused.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { DecisionsProviderData } from '../../api/client/decisions'
import { DecisionsProviderPicker } from './DecisionsProviderPicker'

const SAVED = {
  url: 'https://decider.example.com/v1/decisions',
  service: 'execute-api',
  region: 'us-west-2',
  profile: '',
  model: 'decider-model-v2',
}

function provider(active: string, extra: Partial<DecisionsProviderData> = {}): DecisionsProviderData {
  return {
    presets: [],
    active,
    configured_endpoint: 'https://api.typesafe.ai/v1/systemone',
    runtime: { preset: '', state: 'idle', port: 0, bytes_done: 0, bytes_total: 0, error: '' },
    sagemaker: null,
    sagemaker_available: true,
    aws_https: null,
    aws_https_available: true,
    ...extra,
  }
}

async function renderPicker(data: DecisionsProviderData, consentOn = true) {
  vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue(data)
  vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <DecisionsProviderPicker frozen={false} consentOn={consentOn} />
    </QueryClientProvider>,
  )
  await waitFor(() => expect(client.isFetching()).toBe(0))
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const OPTION = 'Your AWS-signed HTTPS endpoint'

describe('DecisionsProviderPicker AWS-signed HTTPS option', () => {
  it('is not offered by a gateway that does not report it', async () => {
    const { aws_https: _a, aws_https_available: _b, ...older } = provider('jev')
    await renderPicker(older as DecisionsProviderData)
    await screen.findByRole('radio', { name: 'Your SageMaker endpoint' })
    expect(screen.queryByRole('radio', { name: OPTION })).toBeNull()
  })

  it('writes the parts, never an address, once the required ones are filled in', async () => {
    const save = vi.spyOn(api, 'saveDecisionsSignedHttps').mockResolvedValue(provider('aws_https', { aws_https: SAVED }))
    await renderPicker(provider('jev'))
    fireEvent.click(await screen.findByRole('radio', { name: OPTION }))
    const use = screen.getByRole('button', { name: /^Use / })
    expect(use).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Endpoint URL'), { target: { value: ` ${SAVED.url} ` } })
    fireEvent.change(screen.getByLabelText('Signing service name'), { target: { value: 'execute-api' } })
    expect(use).toBeDisabled()
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-west-2' } })
    fireEvent.change(screen.getByLabelText('Model (optional)'), { target: { value: 'decider-model-v2' } })
    fireEvent.click(use)
    await waitFor(() => expect(save).toHaveBeenCalledWith(SAVED))
  })

  it('sends the body the gateway checks, leaving empty optional parts out', async () => {
    const put = vi.fn().mockImplementation(async () => new Response('{}'))
    const { createDecisionsEndpoints } = await import('../../api/client/decisions')
    const endpoints = createDecisionsEndpoints({
      get: vi.fn(),
      post: put,
      put,
      del: vi.fn(),
      j: (r: Response) => r.json(),
    } as never)
    await endpoints.scopesAndFeedback.saveDecisionsSignedHttps({ ...SAVED, model: '' })
    await endpoints.scopesAndFeedback.testDecisionsSignedHttps({ ...SAVED, profile: 'research' })
    expect(put.mock.calls[0]).toEqual([
      '/api/decisions/provider',
      { preset: 'aws_https', url: SAVED.url, service: 'execute-api', region: 'us-west-2' },
    ])
    expect(put.mock.calls[1]).toEqual([
      '/api/decisions/provider/aws-https-test',
      { preset: 'aws_https', url: SAVED.url, service: 'execute-api', region: 'us-west-2', profile: 'research', model: 'decider-model-v2' },
    ])
  })

  it('shows a saved endpoint by its parts, and says nothing is sent before consent', async () => {
    await renderPicker(provider('aws_https', { aws_https: SAVED }), false)
    expect(await screen.findByLabelText('Endpoint URL')).toHaveValue(SAVED.url)
    expect(screen.getByLabelText('Signing service name')).toHaveValue('execute-api')
    expect(screen.getByText(/Not sending decisions to this endpoint yet/)).toHaveTextContent('“Decisions” switch')
    expect(screen.queryByRole('button', { name: /^Use / })).toBeNull()
  })

  it('says which field the gateway refused', async () => {
    const { ApiError } = await import('../../api/apiError')
    vi.spyOn(api, 'saveDecisionsSignedHttps').mockRejectedValue(
      new ApiError('bad', 400, JSON.stringify({ code: 'decisions_provider_aws_https_invalid', field: 'url' })),
    )
    await renderPicker(provider('jev'))
    fireEvent.click(await screen.findByRole('radio', { name: OPTION }))
    fireEvent.change(screen.getByLabelText('Endpoint URL'), { target: { value: 'http://decider.example.com/v1' } })
    fireEvent.change(screen.getByLabelText('Signing service name'), { target: { value: 'execute-api' } })
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-west-2' } })
    fireEvent.click(screen.getByRole('button', { name: /^Use / }))
    expect(await screen.findByText(/Use an https:\/\/ address with a host name and a path/)).toBeInTheDocument()
  })

  it('names the saved endpoint, not Jev, when the administrator refuses it, and offers only to clear it', async () => {
    const save = vi.spyOn(api, 'saveDecisionsProvider').mockResolvedValue(provider('none'))
    await renderPicker(
      provider('none', {
        configured_endpoint: 'aws-https://decider.example.com/v1/decisions?service=execute-api&region=us-west-2',
        hosted_permitted: true,
        provider_permitted: { jev: true, sagemaker: true, aws_https: false },
        configured_refused: true,
      }),
    )
    expect(
      await screen.findByText(/Your AWS-signed HTTPS endpoint is still saved in your settings, but your administrator does not allow it/),
    ).toBeTruthy()
    expect(screen.queryByText(/Jev, hosted by TypeSafe is still saved in your settings/)).toBeNull()
    // Greyed, not choosable, and no option drawn selected over the refused one.
    expect(screen.getByRole('radio', { name: OPTION })).toBeDisabled()
    expect(screen.queryByRole('radio', { checked: true })).toBeNull()
    expect(screen.queryByLabelText('Endpoint URL')).toBeNull()
    const clear = screen.getAllByRole('button', { name: 'Clear from settings' })
    expect(clear).toHaveLength(1)
    fireEvent.click(clear[0])
    await waitFor(() => expect(save).toHaveBeenCalledWith('none'))
  })

  it('keeps a saved endpoint selected when the administrator allows it', async () => {
    await renderPicker(
      provider('aws_https', {
        aws_https: SAVED,
        configured_endpoint: 'aws-https://decider.example.com/v1/decisions?service=execute-api&region=us-west-2',
        provider_permitted: { jev: true, sagemaker: true, aws_https: true },
        configured_refused: false,
      }),
    )
    expect(await screen.findByRole('radio', { name: OPTION })).toBeChecked()
    expect(screen.queryByText(/is still saved in your settings, but your administrator does not allow it/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Clear from settings' })).toBeNull()
  })
})
