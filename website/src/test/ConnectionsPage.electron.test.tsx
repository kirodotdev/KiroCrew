// The approval hand-off in the DESKTOP shell.
//
// A browser tab and the Electron app need opposite mechanics, and the browser
// one is silently dead in the app: every window.open there is arbitrated by the
// main process (electron/external-scheme.js), where the blank target the
// popup-blocker workaround opens first cannot be parsed as a URL and is denied.
// The call returns null, which a browser-only reading takes for a blocked popup,
// so Connect degraded to the fallback link as its only route.
//
// So the shell path waits for the URL and hands THAT to window.open, which the
// main process classifies as cross-origin https and forwards to
// shell.openExternal — the user's real browser, where their provider sessions
// live. It denies the in-app window at the same time, so a null return is this
// path's success shape, not a refusal. There is no user-activation requirement
// to respect: main-process arbitration is not a popup blocker.
//
// `isElectron` is a module-load const derived from the preload bridge, so the
// shell is faked by seeding `window.kirocrew` in a hoisted block — the real
// derivation in src/lib/electron.ts is what runs, exactly as
// App.linuxElectron.test.tsx does it. The browser-side assertions live in
// ConnectionsPage.coverage.test.tsx ("the approval tab") and are untouched by
// this file; both must hold.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { StrictMode } from 'react'
import { fireEvent, waitFor, within } from '@testing-library/react'

import type { ChatMessage, McpServer, RootState } from '../types'

const mcpServers = vi.fn()
const mcpProbe = vi.fn()
const mcpApply = vi.fn()
const mcpCustomAdd = vi.fn()
const mcpCustomGet = vi.fn()
const mcpCustomUpdate = vi.fn()
const mcpOAuthRelay = vi.fn()
const connectionsMint = vi.fn()
const connectionsMintState = vi.fn()
const connectionsPremint = vi.fn()
const connectionsStatus = vi.fn()
const connectionsCancel = vi.fn()
const connectionsDisconnect = vi.fn()
const connectionsTest = vi.fn()

// Must run before src/lib/electron.ts is imported (module-level consts).
vi.hoisted(() => {
  ;(window as unknown as { kirocrew: { isElectron: boolean; platform: string } }).kirocrew = {
    isElectron: true,
    platform: 'darwin',
  }
})

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
  api: {
    mcpServers: (...a: unknown[]) => mcpServers(...a),
    mcpProbe: (...a: unknown[]) => mcpProbe(...a),
    mcpApply: (...a: unknown[]) => mcpApply(...a),
    mcpCustomAdd: (...a: unknown[]) => mcpCustomAdd(...a),
    mcpCustomGet: (...a: unknown[]) => mcpCustomGet(...a),
    mcpCustomUpdate: (...a: unknown[]) => mcpCustomUpdate(...a),
    mcpOAuthRelay: (...a: unknown[]) => mcpOAuthRelay(...a),
    connectionsMint: (...a: unknown[]) => connectionsMint(...a),
    connectionsMintState: (...a: unknown[]) => connectionsMintState(...a),
    connectionsPremint: (...a: unknown[]) => connectionsPremint(...a),
    connectionsStatus: (...a: unknown[]) => connectionsStatus(...a),
    connectionsCancel: (...a: unknown[]) => connectionsCancel(...a),
    connectionsDisconnect: (...a: unknown[]) => connectionsDisconnect(...a),
    connectionsTest: (...a: unknown[]) => connectionsTest(...a),
  },
}))

vi.mock('../pages/overview/McpTab', () => ({ default: () => null }))

import ConnectionsPage from '../pages/connections/ConnectionsPage'
import { isElectron } from '../lib/electron'
import { createTestStore, renderWithProviders } from './helpers'

const NOTION_URL = 'https://mcp.notion.com/mcp'
const MINTED = 'https://mcp.notion.com/authorize?state=desktop'
/** A URL nobody on this page asked for: it arrives on a gateway OAuth banner. */
const BANNER_URL = 'https://mcp.notion.com/authorize?state=banner'

interface ChatSeed {
  messages?: ChatMessage[]
  slotMessages?: Record<string, ChatMessage[]>
}

/** A gateway `mcp_oauth` banner for `serverName`, exactly as chatSlice holds it. */
function banner(serverName: string, meta: Record<string, unknown>): ChatMessage {
  return {
    role: 'mcp_oauth',
    content: '',
    cls: '',
    ts: '2026-03-04T10:00:00.000Z',
    meta: { server_name: serverName, ...meta },
  }
}

function mount({ strict = false, chat = {} }: { strict?: boolean; chat?: ChatSeed } = {}) {
  const store = createTestStore({
    chat: {
      messages: chat.messages ?? [],
      slotMessages: chat.slotMessages ?? {},
    } as unknown as RootState['chat'],
  })
  const tree = <ConnectionsPage servicesEnabled />
  return renderWithProviders(strict ? <StrictMode>{tree}</StrictMode> : tree, { store })
}

function card(slug: string): HTMLElement {
  const el = document.getElementById(`connection-${slug}`)
  if (!el) throw new Error(`no card rendered for ${slug}`)
  return el
}

/**
 * Swap `window.open` by hand rather than with vi.spyOn so the restore is
 * guaranteed by try/finally even when an assertion throws, and return NULL the
 * way the shell's handler does for an external hand-off — the card must read
 * that as success.
 */
const withOpen = async (body: (calls: unknown[][]) => Promise<void>): Promise<void> => {
  const original = window.open
  const calls: unknown[][] = []
  window.open = ((...args: unknown[]) => {
    calls.push(args)
    return null
  }) as typeof window.open
  try {
    await body(calls)
  } finally {
    window.open = original
  }
}

const clickConnect = async () => {
  fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))
}

beforeEach(() => {
  mcpServers.mockReset().mockResolvedValue([])
  mcpProbe.mockReset().mockResolvedValue([])
  mcpApply.mockReset().mockResolvedValue({ ok: true })
  mcpCustomAdd.mockReset().mockResolvedValue({ ok: true, added: [], enabled: true })
  mcpCustomGet.mockReset().mockResolvedValue({ name: 'notion', spec: { url: NOTION_URL }, enabled: true })
  mcpCustomUpdate.mockReset().mockResolvedValue({ ok: true, name: 'notion' })
  mcpOAuthRelay.mockReset().mockResolvedValue({ ok: true })
  connectionsMint.mockReset().mockResolvedValue({ ok: true, slug: 'notion', state: 'minting', token: 'tok1' })
  connectionsMintState.mockReset().mockResolvedValue({ slug: 'notion', state: 'minting', token: 'tok1' })
  connectionsPremint.mockReset().mockResolvedValue({ ok: true, preminting: ['notion'] })
  connectionsStatus.mockReset().mockResolvedValue({ schema_version: 1, connections: [] })
  connectionsCancel.mockReset().mockResolvedValue({ ok: true, slug: 'notion', dropped: true })
  connectionsDisconnect.mockReset().mockResolvedValue({
    ok: true, grantRemoved: true, grantSurviving: [], entryRemoved: true, grantSharedWith: [],
  })
  connectionsTest.mockReset().mockResolvedValue({
    schema_version: 1, slug: 'notion', verdict: 'usable', code: 'tools_available', toolCount: 2,
  })
})

describe('the desktop approval hand-off', () => {
  it('runs against a faked Electron shell', () => {
    // Pins the fake itself: without it every assertion below would silently be
    // testing the browser path and pass for the wrong reason.
    expect(isElectron).toBe(true)
  })

  it('asks for no window on the click', async () => {
    // The blank target is what the shell's handler denies, so sending it is the
    // defect. Nothing is opened until there is a URL to open.
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting' })
    await withOpen(async calls => {
      mount()
      await clickConnect()

      await waitFor(() => expect(connectionsMint).toHaveBeenCalledWith('notion'))
      expect(calls).toEqual([])
    })
  })

  it('hands the full URL to the shell once the mint delivers it', async () => {
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: MINTED })
    await withOpen(async calls => {
      mount()
      await clickConnect()

      await waitFor(() => expect(calls).toHaveLength(1))
      // The whole URL, not a blank placeholder: the main process needs the real
      // target to classify it external and reach shell.openExternal.
      expect(calls[0][0]).toBe(MINTED)
      expect(calls[0][1]).toBe('_blank')
    })
  })

  it('says the approval opened in the default browser, never in a tab', async () => {
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: MINTED })
    await withOpen(async () => {
      mount()
      await clickConnect()

      // A null return is the hand-off's success shape here, so the copy must not
      // fall back to the refused-tab wording -- and must not claim a tab this
      // app never opens either.
      await waitFor(() =>
        expect(within(card('notion')).getByText(/Approval opened in your default browser/)).toBeInTheDocument(),
      )
      expect(within(card('notion')).queryByText(/Finish approving in your browser/)).toBeNull()
    })
  })

  it('keeps the re-open link as the recovery path', async () => {
    // shell.openExternal fails silently by design (openExternalSafely swallows
    // both failure shapes), so the link is the only way back into a hand-off the
    // OS dropped.
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: MINTED })
    await withOpen(async () => {
      mount()
      await clickConnect()

      const link = await waitFor(() =>
        within(card('notion')).getByRole('link', { name: /Re-open approval/ }),
      )
      expect(link).toHaveAttribute('href', MINTED)
    })
  })

  it('opens the browser once when the delivery effect runs twice', async () => {
    // StrictMode double-invokes the effect inside one commit, before any
    // re-render, so state cannot dedupe the hand-off and two browser windows
    // would open on one Connect.
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: MINTED })
    await withOpen(async calls => {
      mount({ strict: true })
      await clickConnect()

      await waitFor(() => expect(calls).toHaveLength(1))
      expect(calls.filter(c => c[0] === MINTED)).toHaveLength(1)
    })
  })

  it('holds the neutral heading while the mint has no URL yet', async () => {
    // Between the click and the URL nothing is open anywhere, so neither the
    // browser-tab claim nor the browser-opened claim is true. Asserted as two
    // absences on purpose: the neutral heading shares its wording with the state
    // badge, so a positive match would not tell them apart.
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting' })
    await withOpen(async () => {
      mount()
      await clickConnect()

      await waitFor(() =>
        expect(within(card('notion')).getAllByText('Waiting for approval').length).toBeGreaterThan(1),
      )
      expect(within(card('notion')).queryByText(/Finish approving in your browser/)).toBeNull()
      expect(within(card('notion')).queryByText(/Approval opened in your default browser/)).toBeNull()
    })
  })
})

// Ownership: only a mint attempt STARTED FROM THIS CARD may auto-open. The browser
// path gets this for free -- it can only fill a tab its own click opened -- while
// the desktop path has no tab to stand in for it, and `approvalUrl` does not carry
// the distinction: a gateway `mcp_oauth` banner (external MCP content) folds a live
// `oauth_url` into the same value. Ungated, that launched the OS browser at a
// provider consent page the user never initiated, and again on every return to the
// page while the banner lived.
describe('the desktop hand-off ownership rule', () => {
  /** A card the gateway already knows about, so the waiting block can render for a
   *  banner that arrived without any click of ours. */
  const configured: McpServer = {
    name: 'notion',
    command: '',
    url: NOTION_URL,
    status: 'unknown',
    source: 'mcp.json',
    enabled: true,
  }
  const bannerOnly = { chat: { messages: [banner('notion', { oauth_url: BANNER_URL })] } }

  it('never opens the browser for a banner-delivered URL', async () => {
    mcpServers.mockResolvedValue([configured])
    await withOpen(async calls => {
      mount(bannerOnly)

      // The card is already in the waiting state from the banner alone, so a
      // hand-off had every chance to fire before this assertion.
      const link = await waitFor(() =>
        within(card('notion')).getByRole('link', { name: /Re-open approval/ }),
      )
      expect(link).toHaveAttribute('href', BANNER_URL)
      expect(calls).toEqual([])
      // No click, so nothing may claim the browser opened either.
      expect(within(card('notion')).queryByText(/Approval opened in your default browser/)).toBeNull()
    })
  })

  it('opens the browser for a URL this card asked for', async () => {
    // The positive half of the same rule: the gate must not cost the feature.
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: MINTED })
    await withOpen(async calls => {
      mount()
      await clickConnect()

      await waitFor(() => expect(calls).toHaveLength(1))
      expect(calls[0][0]).toBe(MINTED)
    })
  })

  it('does not re-open on a remount while the URL is still live', async () => {
    // Navigating away and back re-mounts the card with the banner still live. The
    // ownership signal dies with the attempt that earned it, so the fresh mount has
    // none and the URL is the link's business only -- otherwise every return to the
    // page fires the OS browser again.
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: BANNER_URL })
    await withOpen(async calls => {
      const first = mount()
      await clickConnect()
      await waitFor(() => expect(calls).toHaveLength(1))

      first.unmount()
      mcpServers.mockResolvedValue([configured])
      mount(bannerOnly)

      const link = await waitFor(() =>
        within(card('notion')).getByRole('link', { name: /Re-open approval/ }),
      )
      expect(link).toHaveAttribute('href', BANNER_URL)
      expect(calls).toHaveLength(1)
    })
  })
})
