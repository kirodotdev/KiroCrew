/** Every frame that holds an agent-written page -- a saved artifact, its library
 * thumbnail, a teammate's shared artifact, a chat widget -- is rendered here for
 * real. The thumbnail is checked for its sandbox only (no click reaches it); the
 * others for the same three things:
 *
 *   1. its sandbox is exactly `allow-scripts`: no `allow-popups`, no
 *      `allow-popups-to-escape-sandbox`;
 *   2. the document it mints carries the link-bridge shim and the SAME script
 *      policy as before (the CDN origins are untouched by this change);
 *   3. its host answers the bridge: an open request from that frame, right after
 *      a click in it, opens the URL with `noopener,noreferrer` and no dialog.
 *
 * The popped-out widget tab is checked in WidgetFrame.test.tsx, the shim's own
 * click handling in widgetSrcdocLinkBridge.test.tsx, and the host's URL and
 * gesture rules in hooks/useFrameLinkBridge.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, act } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import type { ReactElement } from 'react'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { ArtifactBodyIframe } from '../components/ArtifactBody'
import { WidgetThumb } from '../components/library/ArtifactThumbs'
import WidgetFrame from '../components/WidgetFrame'
import RemoteArtifactDetailPage from '../pages/RemoteArtifactDetailPage'
import type { Artifact } from '../types'
import { focusEntry, frameInputs, hostInput } from '../hooks/useFrameLinkBridge'

vi.mock('../api/client')
vi.mock('../hooks/useNearViewport', () => ({ useNearViewport: () => true }))

const OPEN = 'kirocrew-dashboard:open'
const LINK = 'https://github.com/example-org/repo/pull/7'
const BODY = `<a href="${LINK}" target="_blank" rel="noopener noreferrer">PR</a>`

beforeEach(() => {
  vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
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
  vi.restoreAllMocks()
})

const surfaces: Array<[string, () => ReactElement, string?]> = [
  ['saved artifact', () => <ArtifactBodyIframe artifact={{ slug: 's', name: 'S', kind: 'html', content: BODY } as unknown as Artifact} />],
  ['chat widget', () => <WidgetFrame html={BODY} title="W" />],
  ['shared artifact', () => (
    <Routes>
      <Route path="/artifacts/remote/:provider/:externalId" element={<RemoteArtifactDetailPage />} />
    </Routes>
  ), '/artifacts/remote/companion/ext-1'],
]

async function frameOf(): Promise<HTMLIFrameElement> {
  return await waitFor(() => {
    const el = document.querySelector('iframe')
    if (!el) throw new Error('the frame never mounted')
    return el as HTMLIFrameElement
  })
}

function mintedDocument(): string {
  const calls = vi.mocked(api.sandboxDocUrl).mock.calls
  if (!calls.length) throw new Error('nothing was minted')
  return String(calls[calls.length - 1][0])
}

describe('the library thumbnail frame', () => {
  // Every render site wraps it in `pointer-events-none` and the frame is
  // `tabIndex={-1}`: no click reaches it, so it carries no link bridge.
  it('grants scripts only, and mints its page without a link shim', async () => {
    renderWithProviders(<WidgetThumb content={BODY} slug="s" />)
    const frame = await frameOf()
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
    expect(frame.getAttribute('tabindex')).toBe('-1')
    const doc = mintedDocument()
    expect(doc).not.toContain(OPEN)
    expect(doc).not.toContain("setAttribute('target', '_blank')")
    expect(doc).toContain('https://cdn.jsdelivr.net https://cdnjs.cloudflare.com')
  })
})

describe.each(surfaces)('the %s frame', (_label, ui, route) => {
  it('grants scripts only, no popups', async () => {
    renderWithProviders(ui(), route ? { route } : undefined)
    const frame = await frameOf()
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
  })

  it('mints its page with the link bridge and the unchanged script policy', async () => {
    renderWithProviders(ui(), route ? { route } : undefined)
    await frameOf()
    const doc = new DOMParser().parseFromString(mintedDocument(), 'text/html')
    const scripts = Array.from(doc.querySelectorAll('script')).map((s) => s.textContent ?? '')
    expect(scripts.some((t) => t.includes(OPEN) && t.includes('window.open = function'))).toBe(true)
    const csp = doc.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute('content') ?? ''
    expect(csp).toContain('https://cdn.jsdelivr.net https://cdnjs.cloudflare.com')
  })

  it('opens a link its page asks for on a real click, with no dialog', async () => {
    const opened = vi.spyOn(window, 'open').mockImplementation(() => null)
    // No click or key in the dashboard itself within an activation's lifespan.
    hostInput.lastAt = -Infinity
    frameInputs.clear()
    renderWithProviders(ui(), route ? { route } : undefined)
    const frame = await frameOf()
    const frameWindow = {} as Window
    Object.defineProperty(frame, 'contentWindow', { configurable: true, value: frameWindow })
    // Focus came into this frame straight from the dashboard.
    focusEntry.frame = frame
    focusEntry.pointer = null
    // A click in the frame: the host has a live activation and its focus is the frame.
    const nav = navigator as Navigator & { userActivation?: { isActive: boolean } }
    Object.defineProperty(nav, 'userActivation', { configurable: true, value: { isActive: true } })
    vi.spyOn(document, 'activeElement', 'get').mockImplementation(() => frame)
    try {
      const event = new MessageEvent('message', { data: { type: OPEN, url: LINK } })
      Object.defineProperty(event, 'source', { value: frameWindow })
      act(() => { window.dispatchEvent(event) })
      expect(opened).toHaveBeenCalledWith(LINK, '_blank', 'noopener,noreferrer')
      expect(screen.queryByRole('dialog')).toBeNull()
    } finally {
      delete (nav as { userActivation?: unknown }).userActivation
    }
  })

  it('offers the Open button for a click it cannot tie to the frame, and the button opens', async () => {
    const opened = vi.spyOn(window, 'open').mockImplementation(() => null)
    hostInput.lastAt = -Infinity
    frameInputs.clear()
    renderWithProviders(ui(), route ? { route } : undefined)
    const frame = await frameOf()
    const frameWindow = {} as Window
    Object.defineProperty(frame, 'contentWindow', { configurable: true, value: frameWindow })
    // Focus reached this frame from another frame, which the dashboard cannot see.
    focusEntry.frame = null
    const nav = navigator as Navigator & { userActivation?: { isActive: boolean } }
    Object.defineProperty(nav, 'userActivation', { configurable: true, value: { isActive: true } })
    const active = vi.spyOn(document, 'activeElement', 'get').mockImplementation(() => frame)
    try {
      const event = new MessageEvent('message', { data: { type: OPEN, url: LINK } })
      Object.defineProperty(event, 'source', { value: frameWindow })
      act(() => { window.dispatchEvent(event) })
      expect(opened).not.toHaveBeenCalled()
      active.mockRestore()
      const button = await screen.findByRole('button', { name: 'Open github.com' })
      expect(button.getAttribute('title')).toBe(LINK)
      act(() => { button.click() })
      expect(opened).toHaveBeenCalledWith(LINK, '_blank', 'noopener,noreferrer')
    } finally {
      delete (nav as { userActivation?: unknown }).userActivation
    }
  })
})
