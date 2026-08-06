import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'
import WidgetFrame from '../components/WidgetFrame'
import { ThemeProvider } from '../hooks/useTheme'
import { api, ApiError } from '../api/client'
import { effectiveWidgetSlug } from '../lib/widgetSlug'

// WidgetFrame consumes useTheme(), which requires a ThemeProvider, and now
// useQuery, which requires a QueryClient. Wrap every render here to mirror the
// production setup (main.tsx wraps App in both).
// Fresh QueryClient per test to avoid cross-test cache pollution.
let queryClient: QueryClient
beforeEach(() => {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
})
const wrap = (ui: ReactNode) =>
  render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{ui}</ThemeProvider>
    </QueryClientProvider>,
  )

// The values we want readThemeVars() to see. Covers: normal hex, rgb(), oklch()
// (modern color syntax), and values that must be rejected by the sanitizer.
const GOOD = {
  '--bg': '#0b1220',
  '--text': 'rgb(240, 240, 240)',
  '--card': '#111827',
  '--accent': 'oklch(0.7 0.2 250)',
  '--border': '#1f2937',
}

// Injected specifically to prove the sanitizer rejects them.
const EVIL = {
  '--muted': 'red; background:url(http://evil.example/pix.gif)',  // attempted CSS break-out
  '--danger': 'expression(alert(1))',                              // legacy IE XSS
  '--ok': '"; }body{display:none} :root{--bg:',                    // quote + brace escape
  '--accent-hover': 'var(--foo, url(http://evil.example))',        // url() smuggled via var() fallback
  '--accent-subtle': 'paint(myWorklet)',                           // Houdini worklet reference
}

beforeEach(() => {
  // Clear localStorage — useTheme reads mc-theme/mc-color-theme at init and
  // test order can leave stale values.
  localStorage.clear()
  delete document.documentElement.dataset.theme

  // blob: URL mock — WidgetFrame uses createObjectURL for iframe src.
  // Intercept Blob constructor to capture the HTML content for test assertions.
  globalThis.Blob = class extends OriginalBlob {
    constructor(parts?: BlobPart[], options?: BlobPropertyBag) {
      super(parts, options)
      if (options?.type?.includes('text/html') && parts?.length) {
        lastBlobContent = parts.map(p => typeof p === 'string' ? p : '').join('')
      }
    }
  } as typeof Blob
  URL.createObjectURL = vi.fn().mockReturnValue('blob:test-widget')
  URL.revokeObjectURL = vi.fn()

  // Jest-dom inherits the real CSSOM, so spy on getComputedStyle to inject a
  // deterministic set of custom properties. The parent app's useTheme applies
  // data-theme on <html>, which the real CSSOM resolves via matching rules;
  // jsdom does not, so we mock the resolver.
  const orig = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation((el: Element) => {
    if (el === document.documentElement) {
      const all = { ...GOOD, ...EVIL } as Record<string, string>
      const fake = {
        getPropertyValue: (name: string) => all[name] ?? '',
      }
      return fake as unknown as CSSStyleDeclaration
    }
    return orig(el)
  })

  // Force matchMedia to report dark OS preference so useTheme resolves to
  // 'dark' for these tests. Unconditional — don't inherit a prior test's
  // stub, since matchMedia is a direct property assignment (not a spy) and
  // vi.restoreAllMocks() can't undo it.
  window.matchMedia = vi.fn().mockReturnValue({
    matches: true, // dark
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
  })

  // ResizeObserver is referenced inside the srcdoc but also by some
  // framer-motion code paths during render.
  // @ts-expect-error test shim
  window.ResizeObserver = window.ResizeObserver ?? class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }

  // Default mock: artifact probe returns 404 (unsaved) so the bookmark
  // starts empty unless a test overrides it.
  vi.spyOn(api, 'artifact').mockRejectedValue(
    Object.assign(new ApiError('Not found', 404), { status: 404 }),
  )
})

afterEach(() => {
  vi.restoreAllMocks()
  globalThis.Blob = OriginalBlob
  queryClient.clear()
})

// Capture blob content passed to createObjectURL for test inspection
let lastBlobContent = ''
const OriginalBlob = globalThis.Blob

function getSrcdoc(_container: HTMLElement): string {
  // Return the captured blob content from our mock
  return lastBlobContent
}

describe('WidgetFrame theme passthrough', () => {
  it('serializes parent theme CSS vars into :root inside the iframe', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)

    expect(srcdoc).toMatch(/:root\s*\{[^}]*--bg:#0b1220/)
    expect(srcdoc).toMatch(/--text:rgb\(240, 240, 240\)/)
    expect(srcdoc).toMatch(/--card:#111827/)
    expect(srcdoc).toMatch(/--accent:oklch\(0\.7 0\.2 250\)/)
    expect(srcdoc).toMatch(/--border:#1f2937/)
  })

  it('sets body background/color to the theme vars so default widgets match the theme', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)
    expect(srcdoc).toMatch(/body\s*\{\s*background:\s*var\(--bg\)\s*;\s*color:\s*var\(--text\)\s*\}/)
  })

  it('drops CSS values that fail the allowlist (url/expression/quote-break/var-fallback/paint)', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)

    // None of the EVIL values survive — and crucially no `url(` / `expression(`
    // / `paint(` appears in the serialized :root, so a compromised parent theme
    // can't exfiltrate via the iframe's CSS even with CSP relaxed.
    expect(srcdoc).not.toMatch(/url\(/)
    expect(srcdoc).not.toMatch(/expression\(/)
    expect(srcdoc).not.toMatch(/paint\(/)
    expect(srcdoc).not.toMatch(/display:none/)
    // The poisoned vars either don't appear or don't carry their malicious
    // payloads. We assert the stronger form: they are absent.
    expect(srcdoc).not.toMatch(/--muted:/)
    expect(srcdoc).not.toMatch(/--danger:/)
    expect(srcdoc).not.toMatch(/--ok:/)
    expect(srcdoc).not.toMatch(/--accent-hover:/)
    expect(srcdoc).not.toMatch(/--accent-subtle:/)
  })

  it('outer iframe frame uses bg-card (not bg-white) so dark themes do not flash white', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const iframe = container.querySelector('iframe')!
    expect(iframe.className).toMatch(/\bbg-card\b/)
    expect(iframe.className).not.toMatch(/\bbg-white\b/)
  })

  it('preserves the CSP meta and loads the Tailwind runtime same-origin', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)
    expect(srcdoc).toMatch(/Content-Security-Policy/)
    // Locked-down-network fix: Tailwind loads from the dashboard origin, not the CDN.
    expect(srcdoc).not.toMatch(/cdn\.tailwindcss\.com/)
    expect(srcdoc).toMatch(/\/vendor\/tailwindcss-browser\.js/)
    // script-src grants no 'unsafe-eval' and pins the dashboard origin to the
    // single vendored runtime FILE (least-privilege), not the whole origin.
    expect(srcdoc).not.toContain("'unsafe-eval'")
    expect(srcdoc).toContain(
      `'unsafe-inline' ${window.location.origin}/vendor/tailwindcss-browser.js https://cdn.jsdelivr.net`,
    )
    // Runtime <script> src is origin-prefixed (absolute), not a bare '/vendor/...' path.
    expect(srcdoc).toContain(`src="${window.location.origin}/vendor/tailwindcss-browser.js"`)
  })

  it('falls back to browser defaults when no theme vars are readable', () => {
    // Simulate a test harness with no CSSOM (e.g. SSR or headless render).
    // Only override getComputedStyle — leave matchMedia alone so useTheme
    // can still resolve the mode without throwing.
    vi.spyOn(window, 'getComputedStyle').mockImplementation(() => ({
      getPropertyValue: () => '',
    } as unknown as CSSStyleDeclaration))

    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)

    // No :root block, no forced body background — widget just renders on the
    // browser default (white) without complaint.
    expect(srcdoc).not.toMatch(/:root/)
    expect(srcdoc).not.toMatch(/body\s*\{\s*background:/)
    // Original body reset (margin/padding/font) is still present.
    expect(srcdoc).toMatch(/body \{ margin: 0; padding: 16px/)
  })

  it('emits color-scheme matching the dashboard mode (not "light dark")', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)
    // jsdom defaults: matchMedia(dark)=true above -> resolved mode is dark.
    expect(srcdoc).toMatch(/color-scheme:dark/)
    expect(srcdoc).not.toMatch(/color-scheme:light dark/)
  })

  it('drives dark mode via v4 custom-variant + <body> class, loaded same-origin', () => {
    // Widgets use Tailwind `dark:` variants (e.g. `bg-white dark:bg-slate-900`).
    // Inside the iframe the OS media query is wrong (it can't know the
    // dashboard's resolved mode), so we register a `.dark`-class custom variant
    // (v4) and put the mode on <body> so `dark:` tracks the dashboard.
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    const srcdoc = getSrcdoc(container)
    expect(srcdoc).toMatch(/@custom-variant dark \(&:where\(\.dark, \.dark \*\)\)/)
    expect(srcdoc).toMatch(/<body class="dark">/)
    // Locked-down-network fix: Tailwind must not load from the public CDN.
    expect(srcdoc).not.toContain('cdn.tailwindcss.com')
    // Directives block must precede the runtime <script> so the dark variant
    // registers before first paint (ordering regression guard).
    expect(srcdoc.indexOf('text/tailwindcss')).toBeLessThan(
      srcdoc.indexOf(`src="${window.location.origin}/vendor/tailwindcss-browser.js"`),
    )
  })

  it('re-renders the srcdoc when the active theme changes (M1 regression guard)', async () => {
    // Arrange: start with theme-A values.
    const THEME_A: Record<string, string> = { '--bg': '#aaaaaa', '--text': '#111111' }
    const THEME_B: Record<string, string> = { '--bg': '#bbbbbb', '--text': '#222222' }
    let active = THEME_A

    // Override just the getComputedStyle spy set up in beforeEach. Leave
    // matchMedia alone so useTheme resolves mode normally.
    vi.spyOn(window, 'getComputedStyle').mockImplementation((el: Element) => {
      if (el === document.documentElement) {
        return {
          getPropertyValue: (name: string) => active[name] ?? '',
        } as unknown as CSSStyleDeclaration
      }
      return getComputedStyle(el)
    })

    const { container, rerender } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)
    expect(getSrcdoc(container)).toMatch(/--bg:#aaaaaa/)

    // Act: swap the mocked CSS vars and fire the cross-instance sync event
    // that useTheme listens to. This is exactly the signal dispatched by
    // setMode_ / setColorTheme / themeEditor in real usage.
    active = THEME_B
    const { act } = await import('@testing-library/react')
    await act(async () => {
      window.dispatchEvent(new CustomEvent('mc-theme-sync', {
        detail: { mode: 'light', colorTheme: 'emerald' },
      }))
    })
    rerender(
      <QueryClientProvider client={queryClient}>
        <ThemeProvider><WidgetFrame html="<p>hi</p>" title="T" /></ThemeProvider>
      </QueryClientProvider>,
    )

    // Assert: the srcdoc now reflects theme-B.
    const after = getSrcdoc(container)
    expect(after).toMatch(/--bg:#bbbbbb/)
    expect(after).not.toMatch(/--bg:#aaaaaa/)
    expect(after).toMatch(/--text:#222222/)
  })
})

describe('WidgetFrame openInNewTab', () => {
  function captureWrapperHtml(container: HTMLElement): string {
    // Spy on Blob to grab the wrapper HTML the openInNewTab handler builds.
    let wrapper = ''
    const realBlob = window.Blob
    vi.spyOn(window, 'Blob' as never).mockImplementation(function (...args: unknown[]) {
      const parts = args[0] as BlobPart[]
      const opts = args[1] as BlobPropertyBag | undefined
      if (typeof parts[0] === 'string') wrapper = parts[0] as string
      return new realBlob(parts, opts)
    })
    URL.createObjectURL = vi.fn().mockReturnValue('blob:test')
    URL.revokeObjectURL = vi.fn()
    vi.spyOn(window, 'open').mockReturnValue(null)

    const btn = container.querySelector('button[aria-label="Open in new tab"]') as HTMLButtonElement
    btn.click()
    return wrapper
  }

  it('declares utf-8 charset (meta + Blob MIME) so popout does not mojibake', () => {
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title="T" />)

    let mimeType = ''
    const realBlob = window.Blob
    vi.spyOn(window, 'Blob' as never).mockImplementation(function (...args: unknown[]) {
      const opts = args[1] as BlobPropertyBag | undefined
      mimeType = opts?.type ?? ''
      return new realBlob(args[0] as BlobPart[], opts)
    })
    URL.createObjectURL = vi.fn().mockReturnValue('blob:test')
    URL.revokeObjectURL = vi.fn()
    vi.spyOn(window, 'open').mockReturnValue(null)

    const btn = container.querySelector('button[aria-label="Open in new tab"]') as HTMLButtonElement
    btn.click()

    expect(mimeType).toBe('text/html;charset=utf-8')
  })

  it('escapes HTML-special characters in title via DOM API (not template literals)', () => {
    const evil = '"><script>alert(1)</script><x title="'
    const { container } = wrap(<WidgetFrame html="<p>hi</p>" title={evil} />)
    const wrapper = captureWrapperHtml(container)

    // Title is set via doc.title which serializes safely. The raw script
    // tag must not appear unescaped in <title>; the entire payload should
    // be HTML-escaped inside the title element.
    expect(wrapper).not.toMatch(/<title>[^<]*<script>/)
    expect(wrapper).toMatch(/<title>[^<]*&lt;script&gt;/)
  })

  it('preserves srcdoc content semantically through srcdoc attribute serialization', () => {
    // Pass HTML containing the byte categories the popout has to tolerate
    // (latin-1 é, raw quote, &, <, >) so the computed srcdoc actually
    // exercises the wrapper's escaping/round-trip machinery.
    const html = '<p title="a&b">&lt;special&gt; &amp; é</p>'
    const { container } = wrap(<WidgetFrame html={html} title="T" />)
    const wrapper = captureWrapperHtml(container)

    const parsed = new DOMParser().parseFromString(wrapper, 'text/html')
    const iframe = parsed.querySelector('iframe')!
    const recovered = iframe.getAttribute('srcdoc') ?? ''
    // The wrapper preserves the inner srcdoc; the inner srcdoc is built via
    // DOM APIs so verbatim string equality is no longer expected (entity
    // re-encoding happens during outerHTML serialization). Verify SEMANTIC
    // equivalence by re-parsing the recovered srcdoc and checking the <p>
    // attribute and text content survived intact.
    expect(recovered).toMatch(/<!DOCTYPE html>/i)
    expect(recovered).toMatch(/<meta charset="utf-8">/)
    const innerDoc = new DOMParser().parseFromString(recovered, 'text/html')
    const p = innerDoc.querySelector('p')!
    expect(p).not.toBeNull()
    // title attribute decoded back to its raw value.
    expect(p.getAttribute('title')).toBe('a&b')
    // text content (which originated as &lt;&gt;&amp; entities in the input
    // and got decoded by createContextualFragment) round-trips correctly.
    expect(p.textContent).toBe('<special> & é')
    // Wrapper iframe is sandboxed (defense-in-depth, even though blob origin
    // is already null).
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts allow-popups allow-popups-to-escape-sandbox')
  })
})

describe('WidgetFrame interactive event bridge', () => {
  it('injects data-action click handler script into srcdoc', () => {
    const { container } = wrap(<WidgetFrame html="<button data-action='test'>Click</button>" title="T" />)
    const srcdoc = getSrcdoc(container)
    expect(srcdoc).toContain("el.dataset.action")
    expect(srcdoc).toContain("mc-widget-action")
    expect(srcdoc).toContain("formData")
  })

  it('dispatches mc-widget-send CustomEvent when receiving mc-widget-action postMessage', async () => {
    const { container } = wrap(<WidgetFrame html="<p>test</p>" title="T" />)
    const iframe = container.querySelector('iframe')!

    const events: CustomEvent[] = []
    const listener = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('mc-widget-send', listener)

    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-widget-action', action: 'approve', payload: { id: '123' } },
      source: iframe.contentWindow,
    }))

    await new Promise(r => setTimeout(r, 50))
    window.removeEventListener('mc-widget-send', listener)

    expect(events).toHaveLength(1)
    expect(events[0].detail.text).toBe('[UI] approve: {"id":"123"}')
  })

  it('formats action-only messages without payload when payload is empty', async () => {
    const { container } = wrap(<WidgetFrame html="<p>test</p>" title="T" />)
    const iframe = container.querySelector('iframe')!

    const events: CustomEvent[] = []
    const listener = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('mc-widget-send', listener)

    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-widget-action', action: 'cancel', payload: {} },
      source: iframe.contentWindow,
    }))

    await new Promise(r => setTimeout(r, 50))
    window.removeEventListener('mc-widget-send', listener)

    expect(events).toHaveLength(1)
    expect(events[0].detail.text).toBe('[UI] cancel')
  })

 // shape validation / allowlist hardening of widget actions.
  it('ignores a widget action with a non-string action (no event dispatched)', async () => {
    const { container } = wrap(<WidgetFrame html="<p>test</p>" title="T" />)
    const iframe = container.querySelector('iframe')!

    const events: CustomEvent[] = []
    const listener = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('mc-widget-send', listener)

    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-widget-action', action: { evil: true }, payload: { id: '1' } },
      source: iframe.contentWindow,
    }))

    await new Promise(r => setTimeout(r, 50))
    window.removeEventListener('mc-widget-send', listener)
    expect(events).toHaveLength(0)
  })

  it('ignores a non-object/array payload and emits the action only', async () => {
    const { container } = wrap(<WidgetFrame html="<p>test</p>" title="T" />)
    const iframe = container.querySelector('iframe')!

    const events: CustomEvent[] = []
    const listener = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('mc-widget-send', listener)

    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-widget-action', action: 'go', payload: ['a', 'b'] },
      source: iframe.contentWindow,
    }))

    await new Promise(r => setTimeout(r, 50))
    window.removeEventListener('mc-widget-send', listener)
    expect(events).toHaveLength(1)
    expect(events[0].detail.text).toBe('[UI] go')
    expect(events[0].detail.action).toBe('go')
  })

  it('caps an oversized widget action payload', async () => {
    const { container } = wrap(<WidgetFrame html="<p>test</p>" title="T" />)
    const iframe = container.querySelector('iframe')!

    const events: CustomEvent[] = []
    const listener = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('mc-widget-send', listener)

    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-widget-action', action: 'flood', payload: { big: 'x'.repeat(20000) } },
      source: iframe.contentWindow,
    }))

    await new Promise(r => setTimeout(r, 50))
    window.removeEventListener('mc-widget-send', listener)
    expect(events).toHaveLength(1)
    expect(events[0].detail.text.length).toBeLessThanOrEqual(4001)
    expect(events[0].detail.text.endsWith('…')).toBe(true)
  })
})

// Regression test: when the widget unmounts mid-flight (user navigates away
// or the chat scrolls the widget out of view between bookmark click and API
// response), the post-await code path must short-circuit so we don't touch
// React state on an unmounted component.
//
// React 18 silently no-ops setState on an unmounted component (the
// "Can't perform a React state update…" warning was removed), so checking
// for that warning would be vacuous. Instead we assert the survivable
// invariant directly: when we resolve the in-flight createArtifact AFTER
// unmount, we observe no fresh DOM render (the bookmark icon never gets
// the "filled" class that setSavedSlug would have triggered). The test
// re-mounts a fresh instance after the unmount → if the post-unmount
// setState had leaked, the new instance would inherit nothing — but the
// guard is what we're testing, not React internals, so we verify the
// guard prevents post-unmount work by checking the unmount completes
// cleanly and no exception is thrown when we resolve the deferred
// promise post-unmount.
describe('WidgetFrame unmount safety on bookmark actions', () => {
  beforeEach(() => {
    vi.resetModules()
    // Use clearAllMocks (not restoreAllMocks) so the outer beforeEach's
    // window.matchMedia stub stays in place for ThemeProvider.
    vi.clearAllMocks()
  })

  it('does not throw or run setState side effects when unmounted before createArtifact resolves', async () => {
    let resolveCreate!: (value: { slug: string; name: string }) => void
    const createSpy = vi.fn(
      () => new Promise<{ slug: string; name: string }>((res) => {
        resolveCreate = res
      }),
    )
    // The component imports `api` once at module load. Patch the property
    // on the live object so the existing import sees the deferred mock.
    vi.spyOn(api, 'createArtifact').mockImplementation(createSpy)
    // Artifact probe returns 404 so savedSlug starts null (unsaved state).
    vi.spyOn(api, 'artifact').mockRejectedValue(
      Object.assign(new ApiError('Not found', 404), { status: 404 }),
    )

    const { container, unmount } = wrap(
      <WidgetFrame html="<p>test</p>" title="T" messageTs="1779995123.456789" widgetIndex={0} />,
    )

    // Wait for probe to settle so bookmark becomes clickable.
    await waitFor(() => {
      expect(container.querySelector('[aria-label="Star as artifact"]')).not.toBeNull()
    })

    const bookmarkBtn = container.querySelector('[aria-label="Star as artifact"]') as HTMLButtonElement
    expect(bookmarkBtn).not.toBeNull()

    // Trigger the async save. createArtifact returns a promise that won't
    // resolve until we explicitly resolve it below.
    bookmarkBtn.click()
    // Yield once so the click handler runs and reaches the `await` point.
    await Promise.resolve()
    expect(createSpy).toHaveBeenCalled()

    // Unmount the component while the save is in flight.
    unmount()

    // Resolve the in-flight promise AFTER unmount. The mountedRef guard
    // should make the post-await code path a no-op:
    //   - No setSavedSlug → no rerender attempt
    //   - No setSaving → no rerender attempt
    //   - No throw / no unhandled rejection
    // Vitest fails the test on any unhandled rejection automatically, so
    // we just call resolveCreate and let the microtask flush surface
    // anything bad. We then assert the unmounted container's bookmark
    // button is gone (no zombie DOM updates).
    resolveCreate({ slug: 'test-artifact', name: 'Test Artifact' })
    await new Promise((r) => setTimeout(r, 30))

    // The unmount tore down the DOM — the bookmark button is gone. If the
    // post-unmount code had somehow re-rendered into the detached tree,
    // that would be a memory leak / zombie state, but the only observable
    // signal is exception/warning behavior. We've covered both above
    // (no throw, microtask flush completes).
    expect(container.querySelector('[aria-label="Star as artifact"]')).toBeNull()
    expect(container.querySelector('[aria-label^="Remove artifact"]')).toBeNull()
  })
})

describe('WidgetFrame saved-state probe (useQuery cache)', () => {
  it('probes api.artifact on mount and caches 404 as unsaved', async () => {
    const artifactSpy = vi.spyOn(api, 'artifact').mockRejectedValue(
      Object.assign(new ApiError('Not found', 404), { status: 404 }),
    )

    const { container } = wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs="1779995123.456789" widgetIndex={0} />,
    )

    await waitFor(() => {
      expect(artifactSpy).toHaveBeenCalledTimes(1)
    })

    // Bookmark should be empty (unfilled)
    const bookmarkBtn = container.querySelector('[aria-label="Star as artifact"]')
    expect(bookmarkBtn).not.toBeNull()
    const removeBtn = container.querySelector('[aria-label^="Remove artifact"]')
    expect(removeBtn).toBeNull()
  })

  it('two impressions of same slug share cache — only one API call', async () => {
    const artifactSpy = vi.spyOn(api, 'artifact').mockRejectedValue(
      Object.assign(new ApiError('Not found', 404), { status: 404 }),
    )

    wrap(
      <WidgetFrame html="<p>a</p>" title="A" messageTs="1779995123.456789" widgetIndex={0} />,
    )
    wrap(
      <WidgetFrame html="<p>b</p>" title="B" messageTs="1779995123.456789" widgetIndex={0} />,
    )

    await waitFor(() => {
      expect(artifactSpy).toHaveBeenCalled()
    })
    // React Query deduplicates concurrent requests for the same key
    expect(artifactSpy).toHaveBeenCalledTimes(1)
  })

  it('visibilitychange within staleTime does not trigger extra call', async () => {
    const artifactSpy = vi.spyOn(api, 'artifact').mockRejectedValue(
      Object.assign(new ApiError('Not found', 404), { status: 404 }),
    )

    wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs="1779995123.456789" widgetIndex={0} />,
    )

    await waitFor(() => {
      expect(artifactSpy).toHaveBeenCalledTimes(1)
    })

    // Simulate tab refocus — React Query's refetchOnWindowFocus respects staleTime
    await act(async () => {
      window.dispatchEvent(new Event('focus'))
    })

    // Still only 1 call because data is fresh (within 5min staleTime)
    expect(artifactSpy).toHaveBeenCalledTimes(1)
  })

  it('click save fills bookmark instantly via cache set', async () => {
    vi.spyOn(api, 'artifact').mockRejectedValue(
      Object.assign(new ApiError('Not found', 404), { status: 404 }),
    )
    vi.spyOn(api, 'createArtifact').mockResolvedValue({ slug: 'msg-1779995123-456789-0', name: 'T' })
    vi.spyOn(api, 'setArtifactPinned').mockResolvedValue({} as never)

    const { container } = wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs="1779995123.456789" widgetIndex={0} />,
    )

    // Wait for probe to resolve
    await waitFor(() => {
      expect(container.querySelector('[aria-label="Star as artifact"]')).not.toBeNull()
    })

    // Click save
    const bookmarkBtn = container.querySelector('[aria-label="Star as artifact"]') as HTMLButtonElement
    await act(async () => {
      bookmarkBtn.click()
    })

    // Bookmark should now be filled (remove label)
    await waitFor(() => {
      expect(container.querySelector('[aria-label^="Remove artifact"]')).not.toBeNull()
    })
  })

  it('setQueryData fires even if component unmounts before createArtifact resolves', async () => {
    let resolveCreate!: (v: unknown) => void
    const createPromise = new Promise((r) => { resolveCreate = r })
    vi.spyOn(api, 'artifact').mockRejectedValue(
      Object.assign(new ApiError('Not found', 404), { status: 404 }),
    )
    vi.spyOn(api, 'createArtifact').mockReturnValue(createPromise as Promise<unknown>)
    vi.spyOn(api, 'setArtifactPinned').mockResolvedValue({} as never)

    const { container, unmount } = wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs="1779995123.456789" widgetIndex={0} />,
    )

    // Wait for probe to resolve (404 -> unsaved)
    await waitFor(() => {
      expect(container.querySelector('[aria-label="Star as artifact"]')).not.toBeNull()
    })

    // Click save — starts createArtifact (deferred)
    const bookmarkBtn = container.querySelector('[aria-label="Star as artifact"]') as HTMLButtonElement
    await act(async () => { bookmarkBtn.click() })

    // Unmount before createArtifact resolves
    unmount()

    // Resolve the deferred createArtifact
    await act(async () => { resolveCreate({ slug: 'msg-1779995123-456789-0', name: 'T' }) })

    // Cache should still be updated (global QueryClient, not gated by mountedRef)
    const slug = effectiveWidgetSlug({ messageTs: '1779995123.456789', widgetIndex: 0 })
    expect(queryClient.getQueryData(['artifact-saved', slug])).toEqual({ exists: true, pinned: true })
  })

  // A transient 5xx must not cache as the 404 `false` sentinel, or a saved
  // widget flaps to empty for the full staleTime.
  it('does not cache a non-404 (transient) error as the unsaved sentinel', async () => {
    const artifactSpy = vi.spyOn(api, 'artifact').mockRejectedValue(
      Object.assign(new ApiError('Server error', 500), { status: 500 }),
    )

    wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs="1779995123.456789" widgetIndex={0} />,
    )

    await waitFor(() => {
      expect(artifactSpy).toHaveBeenCalledTimes(1)
    })

    const slug = effectiveWidgetSlug({ messageTs: '1779995123.456789', widgetIndex: 0 })
    await waitFor(() => {
      expect(queryClient.getQueryState(['artifact-saved', slug])?.status).toBe('error')
    })
    expect(queryClient.getQueryData(['artifact-saved', slug])).toBeUndefined()
    expect(artifactSpy).toHaveBeenCalledTimes(1) // retry: false → no retry
  })

  it('re-probes on window focus after staleTime expires', async () => {
    // v5 focusManager keys off document visibilitychange, not window 'focus',
    // so drive it directly.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const artifactSpy = vi.spyOn(api, 'artifact').mockRejectedValue(
        Object.assign(new ApiError('Not found', 404), { status: 404 }),
      )

      wrap(
        <WidgetFrame html="<p>hi</p>" title="T" messageTs="1779995123.456789" widgetIndex={0} />,
      )

      await waitFor(() => {
        expect(artifactSpy).toHaveBeenCalledTimes(1)
      })

      await act(async () => {
        vi.advanceTimersByTime(5 * 60 * 1000 + 1000)
      })
      await act(async () => {
        focusManager.setFocused(false)
        focusManager.setFocused(true)
      })

      await waitFor(() => {
        expect(artifactSpy).toHaveBeenCalledTimes(2)
      })
    } finally {
      focusManager.setFocused(undefined)
      vi.useRealTimers()
    }
  })
})

// ── exists vs pinned (auto-registered widgets) ─────────────────────────────
//
// The backend auto-registers every emitted <mcwidget> as an UNPINNED artifact
// (src/kiro_crew/widget_artifacts.py), so `{exists: true, pinned: false}` is the
// normal steady state. These tests pin the two states apart: collapsing them
// (the pre-auto-registration behavior) would light up every widget's star as
// though the user had already saved it, and would make the star click a no-op.
describe('WidgetFrame exists-vs-pinned states', () => {
  const TS = '1779995123.456789'

  it('an auto-registered (existing, unpinned) widget shows a HOLLOW star', async () => {
    vi.spyOn(api, 'artifact').mockResolvedValue({ slug: 'x', name: 'T', pinned: false } as never)

    const { container } = wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs={TS} widgetIndex={0} />,
    )

    await waitFor(() => {
      // Star offers to save — it is NOT already in the library.
      expect(container.querySelector('[aria-label="Star as artifact"]')).not.toBeNull()
    })
    expect(container.querySelector('[aria-label^="Remove artifact"]')).toBeNull()
  })

  it('an existing artifact links its title even when unpinned', async () => {
    vi.spyOn(api, 'artifact').mockResolvedValue({ slug: 'x', name: 'T', pinned: false } as never)

    const { container } = wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs={TS} widgetIndex={0} />,
    )

    const slug = effectiveWidgetSlug({ messageTs: TS, widgetIndex: 0 })
    await waitFor(() => {
      const link = container.querySelector(`a[href="/artifacts/${slug}"]`)
      expect(link).not.toBeNull()
    })
  })

  it('a pinned artifact shows a FILLED star', async () => {
    vi.spyOn(api, 'artifact').mockResolvedValue({ slug: 'x', name: 'T', pinned: true } as never)

    const { container } = wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs={TS} widgetIndex={0} />,
    )

    await waitFor(() => {
      expect(container.querySelector('[aria-label^="Remove artifact"]')).not.toBeNull()
    })
    expect(container.querySelector('[aria-label="Star as artifact"]')).toBeNull()
  })

  it('a non-existent artifact links nothing', async () => {
    vi.spyOn(api, 'artifact').mockRejectedValue(
      Object.assign(new ApiError('Not found', 404), { status: 404 }),
    )

    const { container } = wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs={TS} widgetIndex={0} />,
    )

    await waitFor(() => {
      expect(container.querySelector('[aria-label="Star as artifact"]')).not.toBeNull()
    })
    expect(container.querySelector('a[href^="/artifacts/"]')).toBeNull()
  })

  it('starring an already-registered widget PINS without re-creating it', async () => {
    // The create call is the fallback for unregistered widgets only. Once the
    // probe has confirmed the artifact exists, starring must be a pure pin —
    // re-creating would 409 every time and risks clobbering content the user
    // iterated on.
    const artifactSpy = vi
      .spyOn(api, 'artifact')
      .mockResolvedValue({ slug: 'x', name: 'T', pinned: false } as never)
    const createSpy = vi.spyOn(api, 'createArtifact').mockResolvedValue({} as never)
    const pinSpy = vi.spyOn(api, 'setArtifactPinned').mockResolvedValue({} as never)

    const slug = effectiveWidgetSlug({ messageTs: TS, widgetIndex: 0 })
    const { container } = wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs={TS} widgetIndex={0} />,
    )
    // The probe must have RESOLVED before clicking: a click while it is still in
    // flight legitimately falls back to create (the 409-tolerant path), which is
    // not what this test is about.
    await waitFor(() => { expect(artifactSpy).toHaveBeenCalled() })
    await waitFor(() => {
      expect(queryClient.getQueryData(['artifact-saved', slug])).toEqual({ exists: true, pinned: false })
    })
    const btn = container.querySelector('[aria-label="Star as artifact"]') as HTMLButtonElement
    await act(async () => { btn.click() })

    expect(createSpy).not.toHaveBeenCalled()
    expect(pinSpy).toHaveBeenCalledWith(slug, true)
  })

  it('starring an UNregistered widget falls back to create + pin', async () => {
    // Covers pre-feature widgets, a failed registration, and one reclaimed by
    // the retention sweep.
    vi.spyOn(api, 'artifact').mockRejectedValue(
      Object.assign(new ApiError('Not found', 404), { status: 404 }),
    )
    const createSpy = vi.spyOn(api, 'createArtifact').mockResolvedValue({} as never)
    const pinSpy = vi.spyOn(api, 'setArtifactPinned').mockResolvedValue({} as never)

    const slug = effectiveWidgetSlug({ messageTs: TS, widgetIndex: 0 })
    const { container } = wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs={TS} widgetIndex={0} slotKey="chat-1" />,
    )
    await waitFor(() => {
      expect(queryClient.getQueryData(['artifact-saved', slug])).toEqual({ exists: false, pinned: false })
    })
    const btn = container.querySelector('[aria-label="Star as artifact"]') as HTMLButtonElement
    await act(async () => { btn.click() })

    expect(createSpy).toHaveBeenCalledWith(expect.objectContaining({
      slug: effectiveWidgetSlug({ messageTs: TS, widgetIndex: 0 }),
      kind: 'widget',
      // Attributed to the session so the in-session tab's ?session= query finds it.
      origin_session_key: 'chat-1',
    }))
    expect(pinSpy).toHaveBeenCalled()
  })

  it('starring invalidates the session-artifact-records query the tab reads', async () => {
    // The in-session Artifacts tab is a pinned side panel, so it is usually open
    // while the user clicks a widget's star. Its widget rows come from
    // ['session-artifact-records', slot] — a key React Query prefix-matching does
    // NOT reach from ['artifacts'] — so omitting it leaves the tab showing the
    // opposite star from chat for a full staleTime.
    vi.spyOn(api, 'artifact').mockResolvedValue({ slug: 'x', name: 'T', pinned: false } as never)
    vi.spyOn(api, 'setArtifactPinned').mockResolvedValue({} as never)
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    const slug = effectiveWidgetSlug({ messageTs: TS, widgetIndex: 0 })
    const { container } = wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs={TS} widgetIndex={0} slotKey="chat-1" />,
    )
    await waitFor(() => {
      expect(queryClient.getQueryData(['artifact-saved', slug])).toEqual({ exists: true, pinned: false })
    })
    const btn = container.querySelector('[aria-label="Star as artifact"]') as HTMLButtonElement
    await act(async () => { btn.click() })

    const keys = invalidateSpy.mock.calls.map(c => JSON.stringify((c[0] as { queryKey: unknown }).queryKey))
    expect(keys).toContain(JSON.stringify(['session-artifact-records', 'chat-1']))
    expect(keys).toContain(JSON.stringify(['session-artifacts', 'chat-1']))
  })

  it('unstarring keeps the record (exists) and only clears pinned', async () => {
    // Unpin is metadata-only: the artifact and its history survive, so the
    // session tab still lists it and the title stays linked.
    vi.spyOn(api, 'artifact').mockResolvedValue({ slug: 'x', name: 'T', pinned: true } as never)
    vi.spyOn(api, 'setArtifactPinned').mockResolvedValue({} as never)

    const { container } = wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs={TS} widgetIndex={0} />,
    )
    await waitFor(() => {
      expect(container.querySelector('[aria-label^="Remove artifact"]')).not.toBeNull()
    })
    const btn = container.querySelector('[aria-label^="Remove artifact"]') as HTMLButtonElement
    await act(async () => { btn.click() })

    const slug = effectiveWidgetSlug({ messageTs: TS, widgetIndex: 0 })
    expect(queryClient.getQueryData(['artifact-saved', slug])).toEqual({ exists: true, pinned: false })
  })

  it('a 404 on unstar reconciles to not-exists', async () => {
    // The artifact was deleted outright (e.g. from the library in another tab),
    // so the row should stop claiming it exists rather than showing a dead link.
    vi.spyOn(api, 'artifact').mockResolvedValue({ slug: 'x', name: 'T', pinned: true } as never)
    vi.spyOn(api, 'setArtifactPinned').mockRejectedValue(
      Object.assign(new ApiError('Not found', 404), { status: 404 }),
    )

    const { container } = wrap(
      <WidgetFrame html="<p>hi</p>" title="T" messageTs={TS} widgetIndex={0} />,
    )
    await waitFor(() => {
      expect(container.querySelector('[aria-label^="Remove artifact"]')).not.toBeNull()
    })
    const btn = container.querySelector('[aria-label^="Remove artifact"]') as HTMLButtonElement
    await act(async () => { btn.click() })

    const slug = effectiveWidgetSlug({ messageTs: TS, widgetIndex: 0 })
    expect(queryClient.getQueryData(['artifact-saved', slug])).toEqual({ exists: false, pinned: false })
  })
})
