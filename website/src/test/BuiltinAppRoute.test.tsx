import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import BuiltinAppRoute from '../apps/BuiltinAppRoute'


// Mock the registry to avoid loading real page components
vi.mock('../apps/builtinRegistry', async () => {
  // Dynamic import (not require, not a top-level import): vi.mock is hoisted
  // above static imports, so a module-scope `import { lazy }` would risk a TDZ
  // error inside this factory. await import() resolves lazily when the mock runs.
  const { lazy } = await import('react')
  return {
    getBuiltinComponent: (path: string) => {
      if (path === '/test-app') {
        return lazy(() => Promise.resolve({
          default: () => <div data-testid="test-app-page">Test App Content</div>,
        }))
      }
      return undefined
    },
    hasBuiltinComponent: (path: string) => path === '/test-app',
    BUILTIN_COMPONENT_REGISTRY: {},
  }
})


function renderAtPath(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/:builtinApp" element={<BuiltinAppRoute />} />
        <Route path="/chat" element={<div data-testid="chat-page">Chat</div>} />
      </Routes>
    </MemoryRouter>,
  )
}


describe('BuiltinAppRoute', () => {
  it('renders the registered component for a known route', async () => {
    renderAtPath('/test-app')
    const page = await screen.findByTestId('test-app-page')
    expect(page).toBeInTheDocument()
    expect(page.textContent).toBe('Test App Content')
  })

  it('redirects to /chat for unknown routes', () => {
    renderAtPath('/unknown-route')
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
  })
})
