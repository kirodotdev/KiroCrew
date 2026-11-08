// @vitest-environment jsdom
/**
 * Dashboard edit mode's unsent drafts across a staged-page link. The tab swaps the live
 * dashboard for the staged one (and back), which remounts the dashboard document; the
 * request a person had typed in Edit must still be there when the live page returns.
 */
import { Suspense } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { api, type DashboardLineage, type DashboardManifest } from '../api/client'
import { ThemeProvider } from '../hooks/useTheme'
import { createTestStore } from './helpers'
import CrewDashboardTab from '../pages/members/CrewDashboardTab'
import { EDIT_MESSAGE_TYPE, PICKED_MESSAGE_TYPE } from '../pages/members/dashboardEdit'

vi.mock('../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => ({
    url: srcdoc ? `/sandbox-doc/${srcdoc.length}` : null, pending: false, failed: false, stalled: false, retry: vi.fn(),
  }),
}))
vi.mock('../hooks/usePreviewFlag', () => ({ usePreviewFlag: () => true }))
vi.mock('../hooks/usePreviewFlagServerSync', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../hooks/usePreviewFlagServerSync')>()),
  useServerPreviewFlag: () => true,
}))

const LAZY = { timeout: 5000 }
const MANIFEST = {
  id: 'project-report', version: 1, title: 'Project report',
  fields: { accepted: { type: 'number', source: { fold: 'workstreams', path: 'accepted' } } },
} as unknown as DashboardManifest
const LINEAGE: DashboardLineage = {
  path: 'accepted', field: 'accepted', kind: 'fold', type: 'number', found: true, value: 7,
  hops: [{ kind: 'field', field: 'accepted', type: 'number', fold: 'workstreams', path: 'accepted' }],
  edit: { kind: 'fold' },
}

function body(preview: boolean) {
  return {
    instance_version: 3,
    template: { id: 'project-report', version: 1 },
    html: '<!doctype html><p>stored</p>',
    rendered_html: `<!doctype html><p>${preview ? 'staged' : 'live'}</p><script>/* ${EDIT_MESSAGE_TYPE} */</script>`,
    manifest: MANIFEST,
    state: 'live' as const,
    ...(preview ? { preview: true, staged_ms: 1_760_000_000_000 } : {}),
  }
}

function renderTab(preview: boolean) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (on: boolean) => (
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <ThemeProvider>
          <MemoryRouter>
            <Suspense fallback={null}>
              <CrewDashboardTab slug="atlas" member="Atlas" displayName="Atlas" preview={on} />
            </Suspense>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const view = render(tree(preview))
  return { rerender: (on: boolean) => view.rerender(tree(on)) }
}

async function drawFrame() {
  const frame = await screen.findByTestId('crew-dashboard-iframe', undefined, LAZY) as HTMLIFrameElement
  act(() => { fireEvent.load(frame) })
  return frame
}

describe('Edit drafts across the live and staged pages', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(api, 'memberDashboard').mockImplementation(async (_s, _m, _l, preview) => body(!!preview))
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
  })

  it('keeps an unsent change request when a staged-page link comes and goes', async () => {
    const { rerender } = renderTab(false)
    const frame = await drawFrame()
    fireEvent.click(await screen.findByTestId('crew-dashboard-edit', undefined, LAZY))
    await screen.findByTestId('dashboard-edit-capture', undefined, LAZY)
    act(() => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: PICKED_MESSAGE_TYPE, seq: -1, rect: { x: 10, y: 10, w: 30, h: 20 }, block: '', blockRect: null, blockFields: [], path: 'accepted' },
        source: frame.contentWindow,
      }))
    })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'make it blue' } })

    // The person opens a staged-page link, then the live page returns.
    rerender(true)
    await waitFor(() => expect(screen.queryByTestId('dashboard-edit-comment')).toBeNull(), LAZY)
    rerender(false)
    await drawFrame()
    expect(await screen.findByTestId('dashboard-edit-comment', undefined, LAZY)).toHaveValue('make it blue')
  })
})
