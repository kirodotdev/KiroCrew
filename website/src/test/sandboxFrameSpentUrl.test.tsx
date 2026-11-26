/** A frame the browser reloads by itself finds its single-use sandbox-doc url
 * spent and goes white. Every frame here runs script, so its document carries the
 * host's alive reply (`buildSrcdoc({ aliveReply: true })`): the host pings it on
 * each `load`, a 404 cannot answer, and no answer mints a new url
 * (hooks/useFrameAliveRecovery.ts). The hook's retry cap is covered by
 * CrewDynamicDashboard.spentUrl.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import type { ReactElement } from 'react'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { ArtifactBodyIframe } from '../components/ArtifactBody'
import { WidgetThumb } from '../components/library/ArtifactThumbs'
import WidgetFrame from '../components/WidgetFrame'
import RemoteArtifactDetailPage from '../pages/RemoteArtifactDetailPage'
import { FRAME_ALIVE_MESSAGE_TYPE, FRAME_PING_MESSAGE_TYPE } from '../lib/widgetSrcdoc'
import { FRAME_ALIVE_TIMEOUT_MS } from '../hooks/useFrameAliveRecovery'
import type { Artifact } from '../types'

const mint = vi.hoisted(() => ({ retry: vi.fn(), srcdoc: null as string | null }))

vi.mock('../api/client')
vi.mock('../hooks/useNearViewport', () => ({ useNearViewport: () => true }))
vi.mock('../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null | undefined) => {
    if (srcdoc) mint.srcdoc = srcdoc
    return { url: srcdoc ? '/sandbox-doc/test/tok' : null, failed: false, pending: false, stalled: false, retry: mint.retry }
  },
}))

const BODY = '<p>page</p>'

beforeEach(() => {
  mint.retry.mockReset()
  mint.srcdoc = null
  // No server theme: the frames render on the local default.
  vi.mocked(api.themeBoot).mockResolvedValue(null as never)
  vi.mocked(api.remoteArtifactDetail).mockResolvedValue({
    external_id: 'ext-1',
    title: 'Shared page',
    owner: 'someone',
    visibility: 'SHARED',
    content_type: 'text/html',
    current_version: 1,
    content: BODY,
    view_url: 'https://remote.example.com/a/ext-1',
  } as never)
  vi.mocked(api.remoteArtifactComments).mockResolvedValue({ comments: [] } as never)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

const surfaces: Array<[string, () => ReactElement, string?]> = [
  ['saved artifact', () => <ArtifactBodyIframe artifact={{ slug: 's', name: 'S', kind: 'html', content: BODY } as unknown as Artifact} />],
  ['library thumbnail', () => <WidgetThumb content={BODY} slug="s" goalReport={false} />],
  ['chat widget', () => <WidgetFrame html={BODY} title="W" />],
  ['shared artifact', () => (
    <Routes>
      <Route path="/artifacts/remote/:provider/:externalId" element={<RemoteArtifactDetailPage />} />
    </Routes>
  ), '/artifacts/remote/companion/ext-1'],
]

async function mount(ui: () => ReactElement, route?: string) {
  renderWithProviders(ui(), route ? { route } : undefined)
  const frame = await waitFor(() => {
    const el = document.querySelector('iframe')
    if (!el) throw new Error('the frame never mounted')
    return el as HTMLIFrameElement
  })
  // Attach the recovery's load listener before the fake clock is installed.
  await act(async () => {})
  // The test DOM gives a cross-origin frame no window, so the frame gets a stand-in
  // that records the ping and is the `source` its answer comes from.
  const pings: unknown[] = []
  const win = { postMessage: (message: unknown) => pings.push(message) }
  Object.defineProperty(frame, 'contentWindow', { configurable: true, value: win })
  vi.useFakeTimers()
  const reload = () => act(() => { fireEvent.load(frame) })
  const wait = () => act(() => { vi.advanceTimersByTime(FRAME_ALIVE_TIMEOUT_MS) })
  const answerPing = () => act(() => {
    const ev = new MessageEvent('message', { data: { type: FRAME_ALIVE_MESSAGE_TYPE } })
    Object.defineProperty(ev, 'source', { value: win })
    window.dispatchEvent(ev)
  })
  return { pings, reload, wait, answerPing }
}

describe.each(surfaces)('the %s frame reloaded onto its spent url', (_label, ui, route) => {
  it('mints a new url when the reloaded document does not answer', async () => {
    const { pings, reload, wait } = await mount(ui, route)
    expect(mint.srcdoc).toContain(FRAME_PING_MESSAGE_TYPE)
    reload()
    expect(pings).toContainEqual({ type: FRAME_PING_MESSAGE_TYPE })
    wait()
    expect(mint.retry).toHaveBeenCalledTimes(1)
  })

  it('leaves a document that answers alone', async () => {
    const { pings, reload, wait, answerPing } = await mount(ui, route)
    reload()
    expect(pings).toContainEqual({ type: FRAME_PING_MESSAGE_TYPE })
    answerPing()
    wait()
    expect(mint.retry).not.toHaveBeenCalled()
  })
})

describe('the saved artifact notice after a reload onto its spent url', () => {
  it('waits for the automatic re-mint before it offers Show artifact', async () => {
    const [, ui] = surfaces[0]
    const { reload, wait } = await mount(ui)
    reload()
    wait()
    expect(mint.retry).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/no longer showing/i)).toBeNull()
  })
})
