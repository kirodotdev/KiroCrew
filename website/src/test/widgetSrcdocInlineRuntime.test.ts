import { describe, it, expect, afterEach, vi } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'
import { runtimeScriptText, tailwindRuntimeQuery } from '../lib/tailwindRuntimeInline'
import { TAILWIND_RUNTIME_PATH } from '../lib/vendorPaths'

// A document opened as a blob or saved as a file carries the Tailwind runtime
// inside it: its own request for the linked runtime carries no cookie, so a
// cookie-authenticating proxy in front of the dashboard refuses it.

const RUNTIME = '/*tw*/globalThis.__tw=1;'

function scripts(doc: string): HTMLScriptElement[] {
  return Array.from(new DOMParser().parseFromString(doc, 'text/html').querySelectorAll('script'))
}

function runtimeTags(doc: string): HTMLScriptElement[] {
  return scripts(doc).filter((s) => (s.getAttribute('src') ?? '').endsWith(TAILWIND_RUNTIME_PATH))
}

function scriptSrcOf(doc: string): string {
  const csp = new DOMParser().parseFromString(doc, 'text/html')
    .querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute('content') ?? ''
  return csp.split(';').map((d) => d.trim()).find((d) => d.startsWith('script-src ')) ?? ''
}

describe('buildSrcdoc with an inline runtime', () => {
  it('carries the runtime inside the head instead of linking it', () => {
    const doc = buildSrcdoc({ html: '<p class="p-4">x</p>', themeVars: {}, mode: 'dark', inlineRuntime: RUNTIME })
    expect(runtimeTags(doc)).toHaveLength(0)
    const head = new DOMParser().parseFromString(doc, 'text/html').head
    expect(Array.from(head.querySelectorAll('script')).some((s) => s.textContent === RUNTIME)).toBe(true)
  })

  it('links the runtime as before when none is given', () => {
    const doc = buildSrcdoc({ html: '<p>x</p>', themeVars: {}, mode: 'dark' })
    expect(runtimeTags(doc)).toHaveLength(1)
    expect(scripts(doc).some((s) => s.textContent === RUNTIME)).toBe(false)
  })

  it('keeps the nonce on the inline copy when the page runs only host scripts', () => {
    const doc = buildSrcdoc({ html: '<p>x</p>', themeVars: {}, mode: 'light', pageScripts: false, inlineRuntime: RUNTIME })
    const nonce = /'nonce-([0-9a-f]{32})'/.exec(scriptSrcOf(doc))?.[1]
    expect(nonce).toBeTruthy()
    const inline = scripts(doc).find((s) => s.textContent === RUNTIME)
    expect(inline?.getAttribute('nonce')).toBe(nonce)
    expect(runtimeTags(doc)).toHaveLength(0)
  })

  it('writes the bundle verbatim, with no replacement pattern read from it', () => {
    const js = "var a='$&',b='$1',c=\"$'\";"
    const doc = buildSrcdoc({ html: '<p>x</p>', themeVars: {}, mode: 'dark', inlineRuntime: js })
    expect(scripts(doc).some((s) => s.textContent === js)).toBe(true)
  })

  it('keeps the link for a bundle that cannot sit inside a script element', () => {
    const doc = buildSrcdoc({ html: '<p>x</p>', themeVars: {}, mode: 'dark', inlineRuntime: 'var s="<!--<script>";' })
    expect(runtimeTags(doc)).toHaveLength(1)
  })

  it('neutralises a script terminator in the bundle so the element stays whole', () => {
    const doc = buildSrcdoc({ html: '<p>x</p>', themeVars: {}, mode: 'dark', inlineRuntime: 'var s="</SCRIPT>";' })
    expect(scripts(doc).some((s) => s.textContent === 'var s="<\\/SCRIPT>";')).toBe(true)
  })
})

describe('runtimeScriptText', () => {
  it.each(['</script>', '</SCRIPT>', '</Script >', '</sCrIpT/'])('neutralises %s', (close) => {
    expect(runtimeScriptText(`x="${close}"`)).toBe(`x="<\\/${close.slice(2)}"`)
  })

  it('refuses an empty bundle and one that opens an HTML comment', () => {
    expect(runtimeScriptText('')).toBeNull()
    expect(runtimeScriptText('var s="<!--";')).toBeNull()
  })
})

describe('tailwindRuntimeQuery', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('reads the runtime from the dashboard origin with its cookie', async () => {
    const fetchMock = vi.fn(async () => new Response(RUNTIME, { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(tailwindRuntimeQuery.queryFn()).resolves.toBe(RUNTIME)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url.endsWith(TAILWIND_RUNTIME_PATH)).toBe(true)
    expect(init.credentials).toBe('same-origin')
  })

  it('fails on a refused read, so the link stays', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('denied', { status: 403 })))
    await expect(tailwindRuntimeQuery.queryFn()).rejects.toThrow('403')
  })
})
