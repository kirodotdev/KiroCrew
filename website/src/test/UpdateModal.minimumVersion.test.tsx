import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import UpdateModal from '../components/UpdateModal'

function renderModal(mandatory: boolean) {
  const install = vi.fn().mockResolvedValue({ ok: true })
  ;(window as unknown as { updateAPI?: unknown }).updateAPI = { install }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['update-state'], {
    state: 'downloaded',
    version: '1.2.3',
    mandatory,
    minimumSupportedVersion: mandatory ? '1.2.0' : '',
  })
  const result = render(
    <QueryClientProvider client={client}>
      <UpdateModal />
    </QueryClientProvider>,
  )
  return { ...result, install }
}

describe('UpdateModal minimum-supported-version enforcement', () => {
  afterEach(() => {
    cleanup()
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })

  it('removes every dismissal path for a mandatory staged update', async () => {
    const { container } = renderModal(true)
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /later/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /dismiss/i })).toBeNull()

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.getByRole('dialog')).toBeTruthy()

    const backdrop = container.firstElementChild as HTMLElement
    fireEvent.click(backdrop)
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByRole('button', { name: /restart & update/i })).toBeTruthy()
  })

  it('keeps the ordinary update dismissible', async () => {
    renderModal(false)
    expect(await screen.findByRole('dialog')).toBeTruthy()
    const later = screen.getByRole('button', { name: /later/i })
    fireEvent.click(later)
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
