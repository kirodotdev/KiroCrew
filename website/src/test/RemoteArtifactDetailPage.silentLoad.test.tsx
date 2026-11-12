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

  it('keeps focus on the overlay recovery button while its re-mint runs and ignores a second press', async () => {
    // The focus bug: the overlay recovery button took a native `disabled` while
    // the re-mint was in flight. A disabled button leaves the tab order, so a
    // keyboard press drops focus to the page the moment pending turns on. The
    // button must stay focusable (aria-disabled, not disabled) and ignore a
    // second press while busy.
    const { fireEvent } = await import('@testing-library/react')
    renderPage()

    // Explicit ceiling, not Testing Library's implicit 1000ms: the iframe only
    // mounts after renderPage drives remoteArtifactDetail -> srcdoc build ->
    // useSandboxDoc -> sandboxDocUrl, and that chain can take longer than the
    // default under runner load. The mocks resolve immediately, so 2000ms is
    // ample headroom here while staying inside this test's grace budget below.
    await waitFor(() => {
      if (!document.querySelector('iframe')) throw new Error('iframe never mounted')
    }, { timeout: 2000 })
    await waitFor(
      () => expect(screen.getByText(i18nT('components.artifactBody.no_longer_showing'))).toBeTruthy(),
      { timeout: SILENT_LOAD_GRACE_MS + 2000 },
    )

    // A re-mint that never settles so `pending` stays true for the assertions.
    const { act } = await import('@testing-library/react')
    let resolve!: (v: { url: string }) => void
    vi.mocked(api.sandboxDocUrl).mockReturnValueOnce(new Promise((r) => { resolve = r }))
    const btn = screen.getByRole('button', {
      name: new RegExp(i18nT('components.artifactBody.show_artifact'), 'i'),
    })
    btn.focus()
    await act(async () => { fireEvent.click(btn); await Promise.resolve() })

    // While the re-mint runs the button is aria-disabled, NOT native-disabled,
    // and still holds focus. On main the native `disabled` drops focus to body.
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).not.toBeDisabled()
    expect(btn).toHaveAttribute('aria-busy', 'true')
    expect(document.activeElement).toBe(btn)

    // A second press while busy is ignored: the mint was called once by the
    // first click (the mount mint already settled before this click).
    const callsAfterFirst = vi.mocked(api.sandboxDocUrl).mock.calls.length
    fireEvent.click(btn)
    expect(vi.mocked(api.sandboxDocUrl).mock.calls.length).toBe(callsAfterFirst)

    await act(async () => { resolve({ url: '/sandbox-doc/test/tok2' }); await Promise.resolve() })
  })
})
