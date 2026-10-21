/**
 * The worker breadcrumb on a phone: the root's name keeps a readable minimum and
 * a tooltip, the worker's name stays, the status is a dot with its word for
 * readers, and Back keeps its 44x44 target. jsdom has no layout, so the rules are
 * pinned on the classes that carry them; the pod check measures the pixels.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'

const mobile = vi.hoisted(() => ({ value: true }))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => mobile.value }))
vi.mock('../../api/client', () => ({ api: { crewWorkers: vi.fn().mockResolvedValue({ root: 'root', workers: [] }) } }))

import { CrewWorkerBreadcrumb } from './CrewWorkerBreadcrumb'

function renderCrumb() {
  const store = createTestStore({ dashboard: { slots: [{ key: 'root', title: 'New Teammate Welcome Kit', crew_mode_root: true }] } } as never)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}><Provider store={store}>
      <CrewWorkerBreadcrumb root="root" worker={{ key: 'w1', title: 'First-week checklist', running: false }} />
    </Provider></QueryClientProvider>,
  )
}

describe('CrewWorkerBreadcrumb on a phone', () => {
  it('keeps the root readable, names both, and keeps Back a 44px target', () => {
    renderCrumb()
    const rootCrumb = screen.getByTestId('crew-breadcrumb-root')
    expect(rootCrumb).toHaveClass('min-w-[5.5rem]')
    expect(rootCrumb).toHaveAttribute('title', 'New Teammate Welcome Kit')
    const worker = screen.getByTestId('crew-breadcrumb-worker')
    expect(worker).toHaveTextContent('First-week checklist')
    expect(worker).toHaveAttribute('title', 'First-week checklist')
    const back = screen.getByTestId('crew-back-to-conductor')
    expect(back).toHaveClass('max-md:min-h-11', 'max-md:min-w-11')
    // The status word is for readers only on a phone; the dot carries it.
    expect(screen.getByTestId('crew-status').querySelector('.sr-only')).not.toBeNull()
  })

  it('shows the status word on a desktop header', () => {
    mobile.value = false
    renderCrumb()
    expect(screen.getByTestId('crew-status').querySelector('.sr-only')).toBeNull()
    mobile.value = true
  })
})
