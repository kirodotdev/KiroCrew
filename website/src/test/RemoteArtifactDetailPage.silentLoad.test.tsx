/**
 * Component-level proof that the silent-load watch (useSilentLoadWatch) is wired
 * into a real consumer: when the document mint SUCCEEDS but the frame never
 * fires `load`, the surface shows a notice + recovery rather than a silent
 * blank box.
 *
 * This is deliberately a RENDER test, not a header-value assertion. jsdom never
 * fires an iframe `load` for a gateway-served src, which is exactly the silent
 * condition in production: a url in hand, no load event. Waiting past the grace
 * window must surface the notice.
 *
 * Its limit, stated plainly: it proves the NOTICE renders on a silent load. It
 * does NOT prove pixels -- that the artifact would otherwise have rasterized --
 * because a blank-frame paint bug reproduces only in a packaged compositing
 * host, not in jsdom. The paint remedy is the separate translateZ(0) promotion
 * already on main; this guard is about the missing notice, not the paint.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import RemoteArtifactDetailPage from '../pages/RemoteArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { SILENT_LOAD_GRACE_MS } from '../hooks/useSilentLoadWatch'

vi.mock('../api/client')

const PROVIDER = 'companion'
const EXT_ID = 'ext-silent'

const HTML_DETAIL = {
  external_id: EXT_ID,
  title: 'Silent HTML',
  owner: 'someone',
  visibility: 'SHARED',
  content_type: 'text/html',
  current_version: 1,
  content: '<p>hello</p>',
  view_url: 'https://remote.example.com/a/ext-silent',
}

beforeEach(() => {
  // The mint SUCCEEDS -- a url is in hand -- so this is not the failed-mint path.
  vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
  vi.mocked(api.remoteArtifactDetail).mockResolvedValue(HTML_DETAIL as never)
  vi.mocked(api.remoteArtifactComments).mockResolvedValue({ comments: [] } as never)
})

afterEach(() => {
  vi.restoreAllMocks()
})

function renderPage() {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/remote/:provider/:externalId" element={<RemoteArtifactDetailPage />} />
    </Routes>,
    { route: `/artifacts/remote/${PROVIDER}/${EXT_ID}` },
  )
}

describe('RemoteArtifactDetailPage silent-load notice', () => {
  it('shows the silent-load notice when the frame never fires load', async () => {
    renderPage()

    // The iframe mounts once the mint resolves.
    await waitFor(() => {
      if (!document.querySelector('iframe')) throw new Error('iframe never mounted')
    })

    // Within the grace window the notice must be absent -- a frame that just
    // mounted is not yet a silent load.
    expect(screen.queryByText(i18nT('components.artifactBody.no_longer_showing'))).toBeNull()

    // jsdom never fires the iframe `load`, so the real grace timer elapses and
    // the watch reports silent. Mutation: drop the useSilentLoadWatch wiring or
    // its overlay and this notice never appears -- the frame stays blank with
    // no notice, the exact defect this guards.
    await waitFor(
      () => expect(screen.getByText(i18nT('components.artifactBody.no_longer_showing'))).toBeTruthy(),
      { timeout: SILENT_LOAD_GRACE_MS + 2000 },
    )
  })

  it('keeps a recovery affordance when the re-mint rejects after a silent load', async () => {
    // The regression the overlay's old `!failed` guard introduced: a silent load
    // shows "Show artifact"; taking it re-mints; the re-mint REJECTS while the
    // previous (spent) url stays in place (useSandboxDoc keeps it). blobUrl is
    // still truthy, so the frame ternary keeps rendering the blank <iframe> and
    // never reaches its own `failed` branch -- the overlay is the only place
    // recovery can render. If the overlay hides on `failed`, the user is left
    // with a blank box and NO notice and NO recovery. The overlay must persist
    // under `failed`, showing the known-failure copy + Retry.
    const { fireEvent } = await import('@testing-library/react')
    renderPage()

    await waitFor(() => {
      if (!document.querySelector('iframe')) throw new Error('iframe never mounted')
    })
    await waitFor(
      () => expect(screen.getByText(i18nT('components.artifactBody.no_longer_showing'))).toBeTruthy(),
      { timeout: SILENT_LOAD_GRACE_MS + 2000 },
    )

    // Take "Show artifact"; the re-mint rejects. The previous url survives a
    // failed mint, so blobUrl stays truthy and the frame is never torn down.
    vi.mocked(api.sandboxDocUrl).mockRejectedValueOnce(new Error('gateway said no'))
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('components.artifactBody.show_artifact'), 'i') }))

    // Recovery must remain: the failure copy + a Retry, over the still-mounted
    // frame. Without the fix this asserts against an empty overlay and reds.
    await waitFor(() =>
      expect(screen.getByText(i18nT('components.artifactBody.could_not_render'))).toBeTruthy(),
    )
    expect(screen.getByRole('button', { name: new RegExp(i18nT('components.artifactBody.retry'), 'i') })).toBeTruthy()
  })
})
