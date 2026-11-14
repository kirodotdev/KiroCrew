// @vitest-environment jsdom
/**
 * Edit is offered only over a page whose bridge can answer it. The capture layer
 * covers the frame, so over a page that runs no script (or one rendered without the
 * bridge) it would swallow every click and scroll with nothing to answer them.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { api, type DashboardManifest } from '../../api/client'
import { renderWithProviders } from '../../test/helpers'
import CrewDynamicDashboard, { dashboardPage } from './CrewDynamicDashboard'
import { EDIT_MESSAGE_TYPE } from './dashboardEdit'

describe('the page a correction names', () => {
  it("names a package page by its slug and the record's apply counter", () => {
    // The gateway spells it the same way (`member_dashboard_lineage._page_in_force`).
    // The counter, not the package version: a slug deleted and saved again is at
    // version 1 again, but at a later apply.
    expect(dashboardPage({ dashboard: 'report', templateId: 'report', pageRevision: 5 })).toBe('package:report@5')
    expect(dashboardPage({ dashboard: '', templateId: 'project-report', pageRevision: 0 })).toBe('template:project-report')
  })
})

const mintState = vi.hoisted(() => ({ pending: false }))
vi.mock('../../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => ({
    url: srcdoc ? `/sandbox-doc/${srcdoc.length}` : null,
    pending: mintState.pending,
    failed: false,
    stalled: false,
    retry: () => {},
  }),
}))
vi.mock('../../hooks/usePreviewFlag', () => ({ usePreviewFlag: () => true }))
vi.mock('../../hooks/usePreviewFlagServerSync', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../hooks/usePreviewFlagServerSync')>()),
  useServerPreviewFlag: () => true,
}))

const MANIFEST = { id: 'atlas-tree', version: 1, title: 'Tree', fields: [] } as unknown as DashboardManifest
const WITH_BRIDGE = `<!doctype html><p>v1</p><script>/* ${EDIT_MESSAGE_TYPE} */</script>`

function page(over: Record<string, unknown> = {}) {
  return {
    instance_version: 1,
    template: { id: 'atlas-tree', version: 1 },
    html: '<!doctype html><p>stored</p>',
    rendered_html: WITH_BRIDGE,
    manifest: MANIFEST,
    state: 'live' as const,
    ...over,
  }
}

/** How long a query may wait for the lazily imported edit layer to mount. */
const LAZY_LAYER_TIMEOUT_MS = 5000

const DASHBOARD = <CrewDynamicDashboard target={{ kind: 'member', slug: 'atlas', member: 'Atlas' }} displayName="Atlas" />
const mount = () => renderWithProviders(DASHBOARD)

/** The frame on screen reports its document loaded; jsdom never loads one itself. */
async function drawn() {
  const frame = await screen.findByTestId('crew-dashboard-iframe')
  act(() => { fireEvent.load(frame) })
}

describe('the Edit button', () => {
  beforeEach(() => { mintState.pending = false })

  it('waits until the document on screen is drawn', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    await screen.findByTestId('crew-dashboard-iframe')
    // Until the frame loads, what is on screen may still be another document.
    expect(screen.queryByTestId('crew-dashboard-edit')).toBeNull()
    await drawn()
    expect(await screen.findByTestId('crew-dashboard-edit')).toBeInTheDocument()
  })

  it('is withdrawn while the document on screen is not current', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    const { rerender } = mount()
    await drawn()
    await screen.findByTestId('crew-dashboard-edit')
    // A mint still in flight: the frame keeps the old document, so Inspect waits.
    mintState.pending = true
    rerender(DASHBOARD)
    await waitFor(() => expect(screen.queryByTestId('crew-dashboard-edit')).toBeNull())
    mintState.pending = false
    rerender(DASHBOARD)
    expect(await screen.findByTestId('crew-dashboard-edit')).toBeInTheDocument()
  })
  it('is offered over a page that runs script with the bridge in it', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    await drawn()
    const edit = await screen.findByTestId('crew-dashboard-edit')
    // Named for what the mode does first: inspect where values came from.
    expect(edit).toHaveTextContent('Inspect')
    // Shown at rest, not only on hover: every use of edit mode starts here.
    expect(edit.className).not.toMatch(/opacity-0/)
  })

  it('keeps the live/preview marker last in the read key with edit on', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    const { queryClient } = mount()
    await drawn()
    await screen.findByTestId('crew-dashboard-edit')
    // The preview hand-over tells a staged read from a live one by the key's LAST
    // element, so the edit marker must not take that place.
    const keys = queryClient.getQueryCache().getAll().map(q => q.queryKey as unknown[]).filter(k => k.includes('edit'))
    expect(keys.length).toBeGreaterThan(0)
    for (const key of keys) expect(['live', 'preview']).toContain(key[key.length - 1])
  })

  it('is withdrawn while an older page is kept on screen for a newer version', async () => {
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    const { queryClient } = mount()
    await drawn()
    await screen.findByTestId('crew-dashboard-edit')
    // A newer version arrives and waits on probation; the old page stays on screen.
    read.mockResolvedValue(page({ instance_version: 2, rendered_html: `${WITH_BRIDGE}<p>v2</p>` }))
    await act(async () => { await queryClient.refetchQueries({ predicate: q => q.queryKey.includes('live') }) })

    // The gateway traces against the newer version's manifest, so the old page on
    // screen cannot be inspected until the newer one is shown.
    await waitFor(() => expect(screen.queryByTestId('crew-dashboard-edit')).toBeNull())
  })

  it('keeps an open edit layer, and its drafts, when a newer version starts waiting', async () => {
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    const { queryClient } = mount()
    await drawn()
    const edit = await screen.findByTestId('crew-dashboard-edit')
    act(() => { edit.click() })
    // Waits on the lazy import of CrewDashboardEditLayer, which a loaded worker can
    // take past the default 1 s to resolve.
    expect(await screen.findByTestId('dashboard-edit-capture', {}, { timeout: LAZY_LAYER_TIMEOUT_MS })).toBeInTheDocument()
    read.mockResolvedValue(page({ instance_version: 2, rendered_html: `${WITH_BRIDGE}<p>v2</p>` }))
    await act(async () => { await queryClient.refetchQueries({ predicate: q => q.queryKey.includes('live') }) })
    // Wait for the newer read to land in the cache: the candidate is then version 2.
    await waitFor(() => expect(
      queryClient.getQueryCache().getAll().some(q => (q.state.data as { instance_version?: number } | undefined)?.instance_version === 2),
    ).toBe(true))
    // The layer stays mounted: a draft typed in it lives in its own state.
    expect(screen.getByTestId('dashboard-edit-capture')).toBeInTheDocument()
  })

  it('treats a package saved again at the same version as a newer page', async () => {
    // Deleted and saved again: same slug, same version 1, a later apply. Until the
    // replacement is shown, the page on screen keeps its own name, so a correction
    // typed on it is refused rather than written into the replacement.
    const pkg = (revision: number, html: string) => page({ dashboard: 'report', template: { id: 'report', version: 1 }, page_revision: revision, rendered_html: html })
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(pkg(1, WITH_BRIDGE))
    const { queryClient } = mount()
    await drawn()
    await screen.findByTestId('crew-dashboard-edit')
    read.mockResolvedValue(pkg(2, `${WITH_BRIDGE}<p>replacement</p>`))
    await act(async () => { await queryClient.refetchQueries({ predicate: q => q.queryKey.includes('live') }) })
    await waitFor(() => expect(screen.queryByTestId('crew-dashboard-edit')).toBeNull())
  })

  it('is not offered when the page may not run script', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page({ scripts: false }))
    mount()
    await screen.findByTestId('crew-dashboard-iframe')
    expect(screen.queryByTestId('crew-dashboard-edit')).toBeNull()
  })
})
