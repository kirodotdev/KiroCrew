import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import WidgetFrame from '../components/WidgetFrame'
import { WidgetFrameProfileContext, widgetFramePolicy, type WidgetFrameProfile } from '../components/widgetFrameProfile'
import { ThemeProvider } from '../hooks/useTheme'
import { api } from '../api/client'
import { AGENT_PAGE_SANDBOX } from '../lib/widgetSrcdoc'

// The welcome profile (a warm welcome brief's widget): page scripts and clicks,
// no network, no composer pre-fill. The chat profile must stay exactly as it was.

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'sandboxDocUrl').mockResolvedValue({ url: '/sandbox-doc/test/tok' })
})

function mount(profile: WidgetFrameProfile, html: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const ui: ReactNode = (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <WidgetFrameProfileContext.Provider value={profile}>
          <WidgetFrame html={html} title="T" />
        </WidgetFrameProfileContext.Provider>
      </ThemeProvider>
    </QueryClientProvider>
  )
  return render(ui)
}

function minted(): Document {
  const calls = vi.mocked(api.sandboxDocUrl).mock.calls
  if (!calls.length) throw new Error('the frame never minted a document URL')
  return new DOMParser().parseFromString(String(calls[calls.length - 1][0]), 'text/html')
}

const cspOf = (doc: Document) =>
  doc.querySelector('meta[http-equiv="Content-Security-Policy"]')!.getAttribute('content')!

const directive = (csp: string, name: string) =>
  csp.split(';').map(s => s.trim()).find(s => s.startsWith(`${name} `)) ?? ''

async function frameIn(container: HTMLElement): Promise<HTMLIFrameElement> {
  return await waitFor(() => {
    const el = container.querySelector('iframe')
    if (!el) throw new Error('no iframe yet')
    return el as HTMLIFrameElement
  })
}

describe('the welcome widget frame profile', () => {
  it('keeps page scripts but names no CDN or remote origin in its CSP', () => {
    mount('welcome', '<button onclick="this.textContent=1">0</button><script>1</script>')
    const doc = minted()
    const csp = cspOf(doc)
    expect(csp).not.toMatch(/cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com/)
    // The only origin is the dashboard's own, for its Tailwind runtime.
    const remote = (csp.match(/https?:\/\/[^\s;']+/g) ?? []).filter(o => !o.startsWith(window.location.origin))
    expect(remote).toEqual([])
    expect(directive(csp, 'connect-src')).toBe("connect-src 'none'")
    expect(directive(csp, 'img-src')).toBe('img-src data: blob:')
    expect(directive(csp, 'default-src')).toBe("default-src 'none'")
    // Page scripts still run: no nonce gate, inline allowed.
    expect(directive(csp, 'script-src')).toContain("'unsafe-inline'")
    expect(directive(csp, 'script-src')).not.toContain('nonce-')
    expect(doc.body.innerHTML).toContain('<script>1</script>')
  })

  it('carries no link bridge, in the frame or in a popped-out tab', async () => {
    const { container } = mount('welcome', '<a href="https://example.com">x</a>')
    expect(minted().documentElement.outerHTML).not.toContain('kirocrew-dashboard:open')
    let wrapper = ''
    const realBlob = window.Blob
    vi.spyOn(window, 'Blob' as never).mockImplementation(function (...args: unknown[]) {
      const parts = args[0] as BlobPart[]
      if (typeof parts[0] === 'string') wrapper = parts[0] as string
      return new realBlob(parts, args[1] as BlobPropertyBag | undefined)
    })
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:http://localhost:6776/test')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    vi.spyOn(window, 'open').mockReturnValue(null)
    await frameIn(container)
    ;(container.querySelector('button[aria-label="Open in new tab"]') as HTMLButtonElement).click()
    expect(wrapper).toContain('<iframe')
    const scripts = Array.from(new DOMParser().parseFromString(wrapper, 'text/html').body.querySelectorAll(':scope > script'))
    expect(scripts.map(s => s.textContent ?? '').some(t => t.includes('kirocrew-dashboard:open'))).toBe(false)
  })

  it('offers no star, so it is never saved where the chat profile would draw it', async () => {
    const { container } = mount('welcome', '<p>hi</p>')
    await frameIn(container)
    expect(container.querySelector('.lucide-star')).toBeNull()
    expect(widgetFramePolicy('welcome').saveAsArtifact).toBe(false)
  })

  it('never turns a widget action into a composer pre-fill', async () => {
    const { container } = mount('welcome', '<button data-action="approve">Go</button>')
    const iframe = await frameIn(container)
    const sent: Event[] = []
    const listener = (e: Event) => sent.push(e)
    window.addEventListener('mc-widget-send', listener)
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-widget-action', action: 'approve', payload: { id: '1' } },
      source: iframe.contentWindow,
    }))
    await new Promise(r => setTimeout(r, 0))
    window.removeEventListener('mc-widget-send', listener)
    expect(sent).toEqual([])
    expect(iframe.getAttribute('sandbox')).toBe(AGENT_PAGE_SANDBOX)
  })
})

describe('the chat widget frame profile is unchanged', () => {
  it('still has its CDN script origins and link bridge', () => {
    mount('chat', '<p>hi</p>')
    const doc = minted()
    expect(cspOf(doc)).toMatch(/cdn\.jsdelivr\.net/)
    expect(doc.documentElement.outerHTML).toContain('kirocrew-dashboard:open')
    expect(widgetFramePolicy('chat')).toEqual({
      srcdoc: { linkBridge: true, offlineScripts: false, rewriteBareLinks: true },
      linkBridge: true,
      widgetActions: true,
      saveAsArtifact: true,
    })
  })

  it('still offers its star', async () => {
    const { container } = mount('chat', '<p>hi</p>')
    await frameIn(container)
    expect(container.querySelector('.lucide-star')).not.toBeNull()
  })

  it('still turns a widget action into a composer pre-fill', async () => {
    const { container } = mount('chat', '<p>t</p>')
    const iframe = await frameIn(container)
    const sent: Event[] = []
    const listener = (e: Event) => sent.push(e)
    window.addEventListener('mc-widget-send', listener)
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-widget-action', action: 'approve', payload: {} },
      source: iframe.contentWindow,
    }))
    await waitFor(() => expect(sent).toHaveLength(1))
    window.removeEventListener('mc-widget-send', listener)
  })
})
