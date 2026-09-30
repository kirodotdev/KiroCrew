/**
 * The route elements the shell's route table mounts: the lazy-page wrapper (a
 * route-scoped error card with a retry that re-imports, and a reload) and the
 * redirects that keep an old bookmark's query string.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { lazyPage, TasksRedirect, ChatRedirect, OrchestratedRedirect } from '../shell/routes'

function Where() {
  const { pathname, search } = useLocation()
  return <div data-testid="where">{pathname + search}</div>
}

function at(entry: string, path: string, element: React.ReactElement) {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path={path} element={element} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
  return screen.findByTestId('where')
}

describe('shell route redirects', () => {
  it('/tasks lands on /projects with its query', async () => {
    expect(await at('/tasks?x=1', '/tasks', <TasksRedirect />)).toHaveTextContent(/^\/projects\?x=1$/)
  })

  it('an unmatched path lands on /chat with its query', async () => {
    expect(await at('/nowhere?sid=a', '/nowhere', <ChatRedirect />)).toHaveTextContent(/^\/chat\?sid=a$/)
  })

  it('/orchestrated keeps its slug and query, and drops the segment without one', async () => {
    expect(await at('/orchestrated/s1?q=2', '/orchestrated/:slug?', <OrchestratedRedirect />)).toHaveTextContent(/^\/chat\/s1\?q=2$/)
  })

  it('/orchestrated with no slug lands on the chat root', async () => {
    expect(await at('/orchestrated', '/orchestrated/:slug?', <OrchestratedRedirect />)).toHaveTextContent(/^\/chat$/)
  })
})

describe('lazyPage', () => {
  const reload = vi.fn()
  afterEach(() => { vi.restoreAllMocks(); reload.mockReset() })

  it('shows the route-scoped error card when the chunk fails, retries with a fresh import, and reloads on request', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    let attempts = 0
    const Page = lazyPage(() => {
      attempts += 1
      return attempts === 1
        ? Promise.reject(new Error('Failed to fetch dynamically imported module'))
        : Promise.resolve({ default: () => <div data-testid="page">loaded</div> })
    })
    render(<Page />)
    expect(await screen.findByText('Failed to fetch dynamically imported module')).toBeInTheDocument()
    const original = window.location
    Object.defineProperty(window, 'location', { configurable: true, value: { ...original, reload } })
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Reload page' }))
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original })
    }
    expect(reload).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Try Again' }))
    expect(await screen.findByTestId('page')).toHaveTextContent('loaded')
    expect(attempts).toBe(2)
    error.mockRestore()
  })
})
