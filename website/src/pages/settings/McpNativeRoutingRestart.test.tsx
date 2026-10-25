import React from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { ApiError, type NativeRoutingStatus, type NativeSettingsConsent } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { NativeRoutingState, useNativeRoutingRestart } from './McpNativeRoutingRestart'

const KEY = 'pages.mcpManagement.nativeRouting.'

describe('native routing restart', () => {
  it('requires consent, retries the exact mutation once, and waits for its generation', async () => {
    const retry = vi.fn<(consent: NativeSettingsConsent) => Promise<NativeRoutingStatus>>()
      .mockResolvedValue({ status: 'restarting', transition_id: 'new-generation' })
    const refusal = new ApiError(409, 'restart required', JSON.stringify({
      code: 'native_settings_restart_required', confirmation_token: 'exact-change', work: { turns: 1, background: 0 },
    }))
    let hook: ReturnType<typeof useNativeRoutingRestart>
    function Harness() {
      hook = useNativeRoutingRestart()
      return <>{hook.dialog}<button onClick={() => hook.capture(refusal, retry)}>Change routing</button></>
    }
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })
    render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Change routing' }))
    expect(screen.getByText(i18nT(KEY + 'confirm_running'))).toBeTruthy()
    expect(retry).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: i18nT('pages.mcpManagement.cancel') }))
    expect(retry).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Change routing' }))
    fireEvent.click(screen.getByRole('button', { name: i18nT(KEY + 'confirm_apply') }))
    await waitFor(() => expect(retry).toHaveBeenCalledTimes(1))
    expect(retry).toHaveBeenCalledWith({ restart_native_sessions: true, native_settings_confirmation_token: 'exact-change' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(hook!.shouldPoll({ status: 'active', transition_id: 'old-generation' })).toBe(true)
    expect(hook!.shouldPoll({ status: 'activation_failed', transition_id: 'new-generation' })).toBe(false)
    expect(hook!.shouldPoll({ status: 'active', transition_id: 'new-generation' })).toBe(false)
    client.clear()
  })

  it('does not treat unrelated conflicts as restart consent', () => {
    let hook: ReturnType<typeof useNativeRoutingRestart>
    function Harness() { hook = useNativeRoutingRestart(); return hook.dialog }
    const client = new QueryClient()
    render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>)
    const retry = vi.fn()
    act(() => expect(hook!.capture(new ApiError(409, 'overlay owns setting', '{"code":"overlay_owns_enabled"}'), retry)).toBe(false))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(retry).not.toHaveBeenCalled()
    client.clear()
  })

  it.each([true, false, null])('shows the failed step and actual saved=%s without claiming activation', saved => {
    const restart = vi.fn()
    render(<NativeRoutingState status={{ status: 'activation_failed', saved, failed_step: 'save' }}
      onRestart={restart} pending={false} requestError={false} />)
    expect(screen.getByRole('status').textContent).toBe(i18nT(KEY + 'activation_failed'))
    expect(screen.getByText(i18nT(KEY + 'failed_step', {
      step: i18nT(KEY + 'step_save'), saved: i18nT(KEY + (saved === null ? 'change_unknown' : saved ? 'change_saved' : 'change_unsaved')),
    }))).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: i18nT(KEY + 'retry_restart') }))
    expect(restart).toHaveBeenCalledTimes(1)
  })
})
