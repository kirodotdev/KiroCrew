/**
 * `/terminal` (`shell/routes.tsx` `TerminalRoute`): the owner lands on chat with
 * the docked Terminal open; a teammate gets a page saying it is the owner's.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { api } from '../api/client'
import { closeBottomTerminal, isBottomTerminalOpen } from '../hooks/useBottomTerminal'
import { TerminalRoute } from './routes'

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/terminal']}>
        <Routes>
          <Route path="/terminal" element={<TerminalRoute />} />
          <Route path="/chat" element={<div data-testid="chat-page" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  if (isBottomTerminalOpen()) closeBottomTerminal()
})

describe('TerminalRoute', () => {
  it('opens the docked Terminal and lands on chat for the owner', async () => {
    vi.spyOn(api, 'viewerOwnerOk').mockResolvedValue(true)
    mount()
    expect(await screen.findByTestId('chat-page')).toBeInTheDocument()
    expect(isBottomTerminalOpen()).toBe(true)
  })

  it('tells a teammate the Terminal is the Crew owner\'s and opens nothing', async () => {
    vi.spyOn(api, 'viewerOwnerOk').mockResolvedValue(false)
    mount()
    expect(await screen.findByTestId('terminal-owner-only')).toHaveTextContent("Only the Crew's owner can use the Terminal.")
    expect(screen.queryByTestId('chat-page')).toBeNull()
    expect(isBottomTerminalOpen()).toBe(false)
  })

  it('waits for the owner check, so a teammate never flashes the chat', async () => {
    let answer: (v: boolean) => void = () => {}
    vi.spyOn(api, 'viewerOwnerOk').mockReturnValue(new Promise<boolean>(r => { answer = r }))
    mount()
    expect(screen.queryByTestId('chat-page')).toBeNull()
    answer(false)
    await waitFor(() => expect(screen.getByTestId('terminal-owner-only')).toBeInTheDocument())
  })
})
