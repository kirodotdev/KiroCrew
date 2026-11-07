import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import React from 'react'
import AppPage from './AppPage'

// Retry swaps the failure view for the loading line, so the button the user
// pressed unmounts. A retry that fails again must put focus back on Retry.
const { getApp, navigate, params } = vi.hoisted(() => ({ getApp: vi.fn(), navigate: vi.fn(), params: { name: 'alpha' } }))
vi.mock('react-router-dom', () => ({
  useParams: () => params,
  useNavigate: () => navigate,
}))
vi.mock('../api/client', () => ({ api: { getApp } }))
vi.mock('../components/InstalledAppSurface', () => ({
  default: () => <div data-testid="app-surface" />,
}))
vi.mock('../components/ErrorNotice', () => ({
  default: ({ message }: { message: string }) => <div data-testid="error">{message}</div>,
}))

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

afterEach(() => { cleanup(); vi.clearAllMocks(); params.name = 'alpha' })

async function failFirstLoad() {
  getApp.mockRejectedValueOnce(new Error('gateway down'))
  render(<AppPage />)
  return screen.findByRole('button', { name: 'Retry' })
}

describe('AppPage Retry focus', () => {
  it('puts focus back on Retry when the retry fails again', async () => {
    const retry = await failFirstLoad()
    const second = deferred<unknown>()
    getApp.mockReturnValueOnce(second.promise)
    retry.focus()
    fireEvent.click(retry)

    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    await act(async () => { second.reject(new Error('still down')) })

    const again = screen.getByRole('button', { name: 'Retry' })
    expect(screen.getByTestId('error')).toHaveTextContent('still down')
    expect(again).toHaveFocus()
  })

  it('leaves focus alone when the user moved it during the retry', async () => {
    getApp.mockRejectedValueOnce(new Error('gateway down'))
    render(<><button type="button">Elsewhere</button><AppPage /></>)
    const retry = await screen.findByRole('button', { name: 'Retry' })
    const second = deferred<unknown>()
    getApp.mockReturnValueOnce(second.promise)
    retry.focus()
    fireEvent.click(retry)
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' })
    elsewhere.focus()

    await act(async () => { second.reject(new Error('still down')) })

    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(elsewhere).toHaveFocus()
  })

  it('does not refocus a load a route change started during the retry', async () => {
    getApp.mockRejectedValueOnce(new Error('gateway down'))
    const { rerender } = render(<AppPage />)
    const retry = await screen.findByRole('button', { name: 'Retry' })
    const pending = deferred<unknown>()
    getApp.mockReturnValueOnce(pending.promise)
    retry.focus()
    fireEvent.click(retry)

    // The same instance re-renders for another app before the retry settles.
    params.name = 'beta'
    getApp.mockRejectedValueOnce(new Error('beta down'))
    await act(async () => { rerender(<AppPage />) })

    expect(screen.getByTestId('error')).toHaveTextContent('beta down')
    expect(document.activeElement).toBe(document.body)
  })

  it('does not move focus when the retry succeeds', async () => {
    const retry = await failFirstLoad()
    getApp.mockResolvedValueOnce({ name: 'alpha', origin: 'local', manifest: { ui: { entry: 'a.mjs' } } })
    retry.focus()
    await act(async () => { fireEvent.click(retry) })

    expect(screen.getByTestId('app-surface')).toBeInTheDocument()
    expect(document.activeElement).toBe(document.body)
  })
})
