import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../components/settings', async importOriginal => {
  const actual = await importOriginal<typeof import('../components/settings')>()
  return {
    ...actual,
    SettingsSelect: ({
      label,
      value,
      options,
      optionLabels,
      onChange,
      disabled,
      configKey,
    }: {
      label: string
      value: string
      options: string[]
      optionLabels: string[]
      onChange: (value: string) => void
      disabled?: boolean
      configKey?: string
    }) => (
      <select
        aria-label={label}
        value={value}
        disabled={disabled}
        data-setting-key={configKey}
        onChange={event => onChange(event.target.value)}
      >
        {options.map((option, index) => (
          <option key={option} value={option}>{optionLabels[index]}</option>
        ))}
      </select>
    ),
  }
})

const { configMock, patchConfigMock, restartGatewayMock } = vi.hoisted(() => ({
  configMock: vi.fn(),
  patchConfigMock: vi.fn(),
  restartGatewayMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    kirocrewConfig: configMock,
    patchConfig: patchConfigMock,
    restartGateway: restartGatewayMock,
  },
}))

import { AiBackendPanel } from '../pages/settings/AiBackendPanel'

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AiBackendPanel />
    </QueryClientProvider>,
  )
}

function chooseCodex(select: HTMLElement) {
  fireEvent.change(select, { target: { value: 'codex' } })
}

describe('AiBackendPanel', () => {
  beforeEach(() => {
    configMock.mockReset().mockResolvedValue({ agent: { provider: 'acp', acp_backend: '' } })
    patchConfigMock.mockReset().mockImplementation(async (_path: string, value: string) => {
      const result = {
        agent: { provider: 'acp', acp_backend: value },
        restart_required: true,
      }
      configMock.mockResolvedValue(result)
      return result
    })
    restartGatewayMock.mockReset().mockResolvedValue({ ok: true, status: 'restarting' })
  })

  it('shows all selectable ACP backends while keeping Kiro as the default', async () => {
    renderPanel()

    const select = await screen.findByRole('combobox', { name: 'ACP backend' })
    expect(select).toHaveTextContent('Kiro')
    expect(screen.getByRole('option', { name: 'Codex' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Kiro Agent Server' })).toBeInTheDocument()
  })

  it('saves agent.acp_backend and explains the restart boundary', async () => {
    renderPanel()

    chooseCodex(await screen.findByRole('combobox', { name: 'ACP backend' }))

    await waitFor(() => {
      expect(patchConfigMock).toHaveBeenCalledWith('agent.acp_backend', 'codex')
    })
    expect(await screen.findByText('Restart required')).toBeInTheDocument()
    expect(
      screen.getByText(/Restart the gateway, then start a new chat to use this backend/),
    ).toBeInTheDocument()
    expect(screen.getByText(/Existing chats are not migrated in place/)).toBeInTheDocument()
    expect(screen.queryByText(/Existing chats keep their current runtime/)).not.toBeInTheDocument()
    expect(screen.getByText('codex login')).toBeInTheDocument()
  })

  it('uses the established two-step confirmation before restarting the gateway', async () => {
    renderPanel()

    chooseCodex(await screen.findByRole('combobox', { name: 'ACP backend' }))
    const restart = await screen.findByTestId('ai-backend-restart')

    fireEvent.click(restart)
    expect(restartGatewayMock).not.toHaveBeenCalled()
    fireEvent.click(restart)
    await waitFor(() => expect(restartGatewayMock).toHaveBeenCalledTimes(1))
  })

  it('restores the previous selection when the validated write fails', async () => {
    patchConfigMock.mockRejectedValue(new Error('write failed'))
    renderPanel()

    const select = await screen.findByRole('combobox', { name: 'ACP backend' })
    chooseCodex(select)

    expect(await screen.findByText('Failed to save dashboard config')).toBeInTheDocument()
    await waitFor(() => expect(select).toHaveTextContent('Kiro'))
    expect(screen.queryByText('Restart required')).not.toBeInTheDocument()
  })
})
