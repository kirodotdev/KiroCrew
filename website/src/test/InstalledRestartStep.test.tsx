// The installed step a dialog outside the About card shows: one button,
// "Restart to use vX". A restart the gateway accepts (200) can still be refused by its
// worker afterwards; that refusal arrives as an `error` progress step, and the
// step must leave "Restarting" and say why, as the About card does.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { setUpdateProgress } from '../store/dashboardSlice'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { InstalledRestartStep } from '../pages/settings/AboutPanelUpdateFlow'

afterEach(() => vi.restoreAllMocks())

describe('InstalledRestartStep', () => {
  it('is one button naming the installed version, and restarts on the confirm click', async () => {
    const restart = vi.spyOn(api, 'restartGateway').mockResolvedValue({ ok: true } as never)
    renderWithProviders(<InstalledRestartStep version="9.9.9" />, { store: createTestStore() })
    expect(screen.getByTestId('in-app-update-installed').querySelector('p')).toBeNull()
    const button = screen.getByTestId('gateway-restart-installed')
    expect(button).toHaveTextContent('Restart to use v9.9.9')
    fireEvent.click(button)
    expect(button).toHaveTextContent(i18nT('pages.settings.aboutPanel.restart_confirm'))
    expect(restart).not.toHaveBeenCalled()
    fireEvent.click(button)
    await waitFor(() => expect(restart).toHaveBeenCalledTimes(1))
  })

  it('shows a refusal the restart worker sends after the 200, and offers Restart again', async () => {
    vi.spyOn(api, 'restartGateway').mockResolvedValue({ ok: true } as never)
    const store = createTestStore()
    renderWithProviders(<InstalledRestartStep version="9.9.9" />, { store })
    const button = screen.getByTestId('gateway-restart-installed')
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(button).toBeDisabled())
    act(() => { store.dispatch(setUpdateProgress({ step: 'restarting', detail: 'Restarting…' })) })
    act(() => { store.dispatch(setUpdateProgress({ step: 'error', detail: 'sessions did not drain' })) })
    expect(await screen.findByTestId('gateway-restart-installed-error')).toHaveTextContent('sessions did not drain')
    expect(button).not.toBeDisabled()
  })

  it('keeps a refusal that lands before the 200 when the 200 arrives late', async () => {
    let answer: (v: unknown) => void = () => {}
    vi.spyOn(api, 'restartGateway').mockReturnValue(new Promise(r => { answer = r }) as never)
    const store = createTestStore()
    renderWithProviders(<InstalledRestartStep version="9.9.9" />, { store })
    const button = screen.getByTestId('gateway-restart-installed')
    fireEvent.click(button)
    fireEvent.click(button)
    act(() => { store.dispatch(setUpdateProgress({ step: 'restarting', detail: 'Restarting…' })) })
    act(() => { store.dispatch(setUpdateProgress({ step: 'error', detail: 'sessions did not drain' })) })
    expect(await screen.findByTestId('gateway-restart-installed-error')).toHaveTextContent('sessions did not drain')
    await act(async () => { answer({ ok: true }) })
    await waitFor(() => expect(button).not.toBeDisabled())
  })
})
