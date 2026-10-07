import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => ({ api: { projectGitBranches: vi.fn(), projectGitSwitch: vi.fn() } }))

import LazyBranchSwitcher from '../components/LazyBranchSwitcher'

// The real trigger mounts only after React.lazy resolves import('./BranchSwitcher'),
// which a cold, coverage-instrumented run can take well past the 1s default.
const LAZY_SWITCHER_TIMEOUT = { timeout: 5000 }

function mount(props: Parameters<typeof LazyBranchSwitcher>[0]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <LazyBranchSwitcher {...props} />
    </QueryClientProvider>,
  )
}

describe('LazyBranchSwitcher', () => {
  it('shows the branch in a placeholder, then hands over to the real trigger', async () => {
    mount({ projectDir: '/workspace/project', branch: 'develop', variant: 'chip' })
    expect(screen.getByTestId('branch-switcher-placeholder')).toHaveTextContent('develop')
    // Waits for React.lazy -> import('./BranchSwitcher') -> the real trigger's first render.
    const trigger = await screen.findByTestId('branch-switcher-trigger', {}, LAZY_SWITCHER_TIMEOUT)
    expect(trigger).toHaveTextContent('develop')
    expect(trigger).toHaveAttribute('data-variant', 'chip')
    expect(screen.queryByTestId('branch-switcher-placeholder')).toBeNull()
  })

  it('passes every prop through to the switcher', async () => {
    mount({ projectDir: '/workspace/project', branch: 'main', switchBlocked: true })
    // Waits for React.lazy -> import('./BranchSwitcher') -> the real trigger's first render.
    const trigger = await screen.findByTestId('branch-switcher-trigger', {}, LAZY_SWITCHER_TIMEOUT)
    expect(trigger).toHaveAttribute('data-variant', 'header')
    expect(trigger).toHaveAttribute('data-mode', 'copy')
  })

  it('keeps the hosts behind the import() boundary', () => {
    // A static import of BranchSwitcher from either host pulls the picker back
    // into the always-loaded chat-route chunk and over its size budget.
    for (const host of ['ChatInput.tsx', 'GitPanel.tsx']) {
      const src = readFileSync(resolve(__dirname, '../components', host), 'utf8')
      expect(src, host).not.toMatch(/from '\.\/BranchSwitcher'/)
      expect(src, host).toMatch(/from '\.\/LazyBranchSwitcher'/)
    }
  })
})
