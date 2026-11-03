import { describe, expect, it } from 'vitest'
import {
  applyPushedRead,
  buildDashboardDownload,
  DashboardDownloadError,
  downloadFileName,
  MAX_DOWNLOAD_BYTES,
  STATIC_PAGE_SANDBOX,
  withIslandRead,
} from './dashboardDownloadFile'
import { CREW_DASHBOARD_SANDBOX_STATIC, islandRead } from './CrewDynamicDashboard'
import { escapeScriptClose } from '../../lib/widgetSrcdoc'
import { TAILWIND_RUNTIME_PATH } from '../../lib/vendorPaths'
import { withBase } from '../../lib/basePath'

const RUNTIME_URL = window.location.origin + withBase(TAILWIND_RUNTIME_PATH)
const PAGE = '<div data-dashboard-field="open">3</div><script>window.drawn = 1</script>'
const THEME = { '--bg': '#101010', '--text': '#eeeeee' }

async function saved(pageScripts: boolean, runtime = 'self.tw = 1'): Promise<string> {
  const blob = await buildDashboardDownload(
    { html: PAGE, themeVars: THEME, mode: 'dark', pageScripts },
    async (url) => {
      expect(url).toBe(RUNTIME_URL)
      return runtime
    },
  )
  return blob.text()
}

/** The saved file's one frame, read back through a parser. */
function wrapperFrame(file: string): HTMLIFrameElement {
  const frames = new DOMParser().parseFromString(file, 'text/html').querySelectorAll('iframe')
  expect(frames).toHaveLength(1)
  return frames[0]
}

/** The page document a save carries, read from its frame's `srcdoc`. */
async function savedPage(runtime?: string, pageScripts = true): Promise<string> {
  return wrapperFrame(await saved(pageScripts, runtime)).getAttribute('srcdoc') ?? ''
}

describe('buildDashboardDownload', () => {
  it('saves a scripted page in a frame sandboxed as the tab frame is', async () => {
    const file = await saved(true)
    const frame = wrapperFrame(file)
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
    // The srcdoc round-trips to the document the page is built as.
    const page = frame.getAttribute('srcdoc') ?? ''
    expect(page).toContain('<script>window.drawn = 1</script>')
    expect(page).toContain('<script>self.tw = 1</script>')
    expect(page).toContain("connect-src 'none'")

    expect(file.startsWith('<!DOCTYPE html>')).toBe(true)
    const wrapper = new DOMParser().parseFromString(file, 'text/html')
    expect(wrapper.head.querySelector('meta[name="viewport"]')?.getAttribute('content')).toBe('width=device-width, initial-scale=1')
    // The wrapper holds no script of its own; the page's script is only inside the frame.
    expect(wrapper.querySelectorAll('script')).toHaveLength(0)
    const cspOf = (d: Document) =>
      d.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute('content') ?? ''
    const csp = cspOf(wrapper)
    // The page's own policy: the srcdoc inherits the wrapper's, so it may not be narrower.
    expect(csp).toBe(cspOf(new DOMParser().parseFromString(page, 'text/html')))
    // No frame navigation to any URL: frame-src and child-src fall back to default-src.
    expect(csp).toContain("default-src 'none'")
    expect(csp).not.toMatch(/(?:frame|child)-src/)
    expect(csp).not.toContain('navigate-to')
    // Painted in the page's background, so opening the file does not flash.
    const rootStyle = wrapper.documentElement.getAttribute('style') ?? ''
    expect(rootStyle).toContain('color-scheme:dark')
    expect(rootStyle).toContain('background:#101010')
  })

  it('saves a page shown without script in a frame with the empty sandbox, as the tab frame is', async () => {
    const file = await saved(false)
    const frame = wrapperFrame(file)
    // Present and empty: every restriction, so no script, top navigation or popup,
    // and a nested frame inherits it.
    expect(frame.hasAttribute('sandbox')).toBe(true)
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(STATIC_PAGE_SANDBOX).toBe(CREW_DASHBOARD_SANDBOX_STATIC)
    const page = frame.getAttribute('srcdoc') ?? ''
    expect(page).toContain('data-dashboard-field="open"')
    expect(page).toMatch(/script-src 'nonce-[^']+'/)
    const wrapper = new DOMParser().parseFromString(file, 'text/html')
    expect(wrapper.querySelectorAll('script')).toHaveLength(0)
    expect(wrapper.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute('content')).toContain(
      "default-src 'none'",
    )
  })

  it('inlines the runtime and loads nothing by URL', async () => {
    const doc = await savedPage()
    expect(doc).toContain('<script>self.tw = 1</script>')
    expect(doc).not.toMatch(/<script[^>]*\ssrc=/)
    expect(doc).not.toContain('x-script-placeholder')
  })

  it('keeps the page, its theme and the frame CSP', async () => {
    const doc = await savedPage()
    expect(doc).toContain('data-dashboard-field="open"')
    expect(doc).toContain('--bg')
    expect(doc).toContain('#101010')
    expect(doc).toContain('<body class="dark"')
    expect(doc).toMatch(/http-equiv="Content-Security-Policy"/)
    expect(doc).toContain("connect-src 'none'")
  })

  it('saves a page shown without script without its script', async () => {
    const doc = await savedPage(undefined, false)
    const nonce = /script-src 'nonce-([^']+)'/.exec(doc)?.[1]
    expect(nonce).toBeTruthy()
    // The runtime is carried under the document's nonce; the page's script has none.
    expect(doc).toContain(`<script nonce="${nonce}">self.tw = 1</script>`)
    expect(doc).not.toContain("script-src 'unsafe-inline'")
  })

  it('escapes every casing of </script in the runtime', async () => {
    const doc = await savedPage('a("</script>");b("</SCRIPT>")')
    expect(doc).toContain('a("<\\/script>");b("<\\/SCRIPT>")')
  })

  it('treats a $ in the runtime as text', async () => {
    const doc = await savedPage("x.replace(/a/, '$&$1')")
    expect(doc).toContain("x.replace(/a/, '$&$1')")
  })

  it('refuses when the runtime cannot be read', async () => {
    await expect(saved(true, '')).rejects.toMatchObject({ reason: 'no_runtime' })
  })

  it('refuses a document over the size cap', async () => {
    await expect(saved(true, 'x'.repeat(MAX_DOWNLOAD_BYTES))).rejects.toBeInstanceOf(DashboardDownloadError)
    await expect(saved(true, 'x'.repeat(MAX_DOWNLOAD_BYTES))).rejects.toMatchObject({ reason: 'too_large' })
  })
})

describe('escapeScriptClose', () => {
  it('escapes every casing of </script and leaves the rest as it is', () => {
    expect(escapeScriptClose('a</script>b</SCRIPT>c</ScRiPt')).toBe('a<\\/script>b<\\/SCRIPT>c<\\/ScRiPt')
    expect(escapeScriptClose("x.replace(/a/, '$&') < 3")).toBe("x.replace(/a/, '$&') < 3")
    expect(escapeScriptClose('')).toBe('')
  })
})

describe('downloadFileName', () => {
  // Built from LOCAL components, so the test reads the same in every timezone.
  const day = new Date(2026, 10, 3, 12, 0, 0)

  it('names the file after the bot and the day', () => {
    expect(downloadFileName('Build watcher', day)).toBe('Build watcher 2026-11-03.html')
  })

  it('takes out characters a file system may refuse', () => {
    expect(downloadFileName('a/b:c*?"<>|\u0007d', day)).toBe('a b c d 2026-11-03.html')
  })

  it('falls back when nothing is left', () => {
    expect(downloadFileName('///', day)).toBe('dashboard 2026-11-03.html')
  })

  it('dates the file by the local day at either end of it', () => {
    expect(downloadFileName('a', new Date(2026, 10, 3, 23, 59, 0))).toBe('a 2026-11-03.html')
    expect(downloadFileName('a', new Date(2026, 0, 9, 0, 1, 0))).toBe('a 2026-01-09.html')
  })
})

const ISLAND_OPEN = '<script type="application/json" id="kirocrew-dashboard-data">'
const HELD = { fields: { open: 3, closed: 1 }, agentic: [], seq: 4, missing: [], version: 2, generation: 'g1', subject: 'crewmate', last_user_at: 'yesterday' }
const COMPOSED = `<style>x</style>\n${ISLAND_OPEN}${JSON.stringify(HELD)}</script>\n<div data-dashboard-field="open">3</div>`

describe('applyPushedRead', () => {
  const patch = (version: number, fields: Record<string, unknown>, generation = 'g1') =>
    ({ patch: true, fields, agentic: [], seq: 5, missing: [], version, generation, subject: 'crewmate', last_user_at: '' })

  it('merges an in-order patch over the fields it did not move', () => {
    const next = applyPushedRead(HELD, patch(3, { open: 7 }))
    expect(next.fields).toEqual({ open: 7, closed: 1 })
    expect(next.version).toBe(3)
    // Kept from the held read, as the frame keeps them.
    expect(next.last_user_at).toBe('yesterday')
  })

  it('drops a patch that skips a version or comes from another generation', () => {
    expect(applyPushedRead(HELD, patch(4, { open: 7 }))).toBe(HELD)
    expect(applyPushedRead(HELD, patch(3, { open: 7 }, 'g2'))).toBe(HELD)
  })

  it.each([
    { generation: 'g2', rejectedVersion: 3, recoveredVersion: 1 },
    { generation: 'g1', rejectedVersion: 4, recoveredVersion: 4 },
  ])('recovers from a rejected patch in $generation and accepts the next patch', ({ generation, rejectedVersion, recoveredVersion }) => {
    const rejected = applyPushedRead(HELD, patch(rejectedVersion, { open: 7 }, generation))
    expect(rejected).toBe(HELD)

    const full = { ...HELD, fields: { open: 9, closed: 6 }, version: recoveredVersion, generation }
    const recovered = applyPushedRead(rejected, full)
    expect(recovered).toBe(full)
    expect(islandRead(withIslandRead(COMPOSED, recovered))?.fields).toEqual({ open: 9, closed: 6 })

    const next = applyPushedRead(recovered, patch(recoveredVersion + 1, { open: 10 }, generation))
    const saved = islandRead(withIslandRead(COMPOSED, next))
    expect(saved?.fields).toEqual({ open: 10, closed: 6 })
    expect(saved?.generation).toBe(generation)
    expect(saved?.version).toBe(recoveredVersion + 1)
  })

  it('takes a newer full read whole and drops a superseded one', () => {
    const full = { fields: { open: 9 }, version: 5, generation: 'g1' }
    expect(applyPushedRead(HELD, full)).toBe(full)
    expect(applyPushedRead(HELD, { ...full, version: 1 })).toBe(HELD)
  })
})

describe('withIslandRead', () => {
  it('puts the pushed read in the island and leaves the rest of the page', () => {
    const read = applyPushedRead(HELD, { patch: true, fields: { open: 7 }, version: 3, generation: 'g1' })
    const html = withIslandRead(COMPOSED, read)
    expect(islandRead(html)?.fields).toEqual({ open: 7, closed: 1 })
    expect(html.startsWith('<style>x</style>\n')).toBe(true)
    expect(html.endsWith('\n<div data-dashboard-field="open">3</div>')).toBe(true)
  })

  it('escapes a value that would end the island early', () => {
    const html = withIslandRead(COMPOSED, { fields: { open: '</script><b>$&' } })
    expect(html).not.toContain('</script><b>')
    expect(islandRead(html)?.fields).toEqual({ open: '</script><b>$&' })
  })

  it('finds the island by its id, whatever the order of its attributes', () => {
    const reordered = COMPOSED.replace(ISLAND_OPEN, "<script id='kirocrew-dashboard-data' type='application/json'>")
    const html = withIslandRead(reordered, { ...HELD, fields: { open: 9 } })
    expect(islandRead(html)?.fields).toEqual({ open: 9 })
  })

  it('leaves a page without an island as it is', () => {
    expect(withIslandRead('<p>no island</p>', HELD)).toBe('<p>no island</p>')
  })

  it('saves the pushed value, not the composed one', async () => {
    const read = applyPushedRead(HELD, { patch: true, fields: { open: 7 }, version: 3, generation: 'g1' })
    const blob = await buildDashboardDownload(
      { html: withIslandRead(COMPOSED, read), themeVars: THEME, mode: 'dark', pageScripts: true },
      async () => 'self.tw = 1',
    )
    const saved = islandRead(wrapperFrame(await blob.text()).getAttribute('srcdoc') ?? '')
    expect(saved?.fields).toEqual({ open: 7, closed: 1 })
    expect(saved?.version).toBe(3)
  })
})
