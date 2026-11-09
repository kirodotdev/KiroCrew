import { readFileSync } from 'node:fs'
import path from 'node:path'

// The shell's pre-boot failure panel. This is the one surface that can still
// appear when a boot-critical module never arrives: no app code has run, so no
// React error boundary exists, and src/lib/staleShellHeal.ts cannot reach it
// either because that is a boot-TIME probe. Before it existed, a top-level page
// in this state showed the dark empty shell and nothing else -- the mobile black
// screen behind PRs #9518 and #9540.
//
// Like serviceWorkerSkipRules.test.ts, this runs the REAL inline script out of
// index.html rather than grepping it, so logic that is present but unreachable
// still fails.

const INDEX_PATH = path.resolve(__dirname, '..', '..', 'index.html')

/** The inline head script that declares revealBootFailure and the error hook. */
function bootScript(): string {
  const html = readFileSync(INDEX_PATH, 'utf8')
  const start = html.indexOf('var bootFailureTimer')
  expect(start).toBeGreaterThan(-1)
  const end = html.indexOf('</scr' + 'ipt>', start)
  expect(end).toBeGreaterThan(start)
  return html.slice(start, end)
}

/** The body script that wires "Clear cache and reload". */
function retryScript(): string {
  const html = readFileSync(INDEX_PATH, 'utf8')
  const start = html.indexOf('var kcRetry')
  expect(start).toBeGreaterThan(-1)
  const end = html.indexOf('</scr' + 'ipt>', start)
  expect(end).toBeGreaterThan(start)
  return html.slice(start, end)
}

/** The panel's opening tag, straight out of the shell markup. */
function panelTag(): string {
  const html = readFileSync(INDEX_PATH, 'utf8')
  const start = html.indexOf('<div id="boot-failure"')
  expect(start).toBeGreaterThan(-1)
  const end = html.indexOf('>', start)
  expect(end).toBeGreaterThan(start)
  return html.slice(start, end + 1)
}

/** The slice of sessionStorage the shell's reload counter uses. */
type FakeStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

/** A working sessionStorage, shared by every simulated load of one tab. */
function memoryStorage(): FakeStorage & { data: Map<string, string> } {
  const data = new Map<string, string>()
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => {
      data.set(key, String(value))
    },
    removeItem: (key) => {
      data.delete(key)
    },
  }
}

type Node = {
  id: string
  hidden: boolean
  style: { display: string }
  firstChild: unknown
  textContent: string
  focused: boolean
  focus: () => void
}

/**
 * A DOM stub with just the nodes the panel logic touches. One call is one page
 * load; pass the same `storage` to several calls to simulate one tab reloading.
 */
function harness(opts: {
  rootHasChildren: boolean
  embedded: boolean
  storage?: FakeStorage
  title?: string
}) {
  const make = (id: string, firstChild: unknown = null, hidden = false): Node => {
    const node: Node = {
      id,
      hidden,
      style: { display: '' },
      firstChild,
      textContent: '',
      focused: false,
      focus: () => {
        node.focused = true
      },
    }
    return node
  }
  const nodes: Record<string, Node> = {
    root: make('root', opts.rootHasChildren ? {} : null),
    'boot-failure': make('boot-failure', null, true),
    'boot-failure-title': make('boot-failure-title'),
    'boot-failure-why': make('boot-failure-why'),
    'boot-failure-next-fetch': make('boot-failure-next-fetch'),
    'boot-failure-next-build': make('boot-failure-next-build', null, true),
    'boot-failure-detail': make('boot-failure-detail'),
    'boot-failure-retry': make('boot-failure-retry'),
  }
  let listener: ((e: unknown) => void) | null = null
  let rejectionListener: ((e: unknown) => void) | null = null
  let domContentLoaded: (() => void) | null = null
  let reloads = 0
  const delays: number[] = []
  const timers: Array<() => void> = []
  const intervals: Array<{ fn: () => void; live: boolean }> = []
  const observers: Array<{ fn: () => void; target: unknown; live: boolean }> = []
  const posted: unknown[] = []

  const win = {
    self: {} as object,
    top: {} as object,
    addEventListener: (kind: string, fn: (e: unknown) => void) => {
      if (kind === 'error') listener = fn
      if (kind === 'unhandledrejection') rejectionListener = fn
      if (kind === 'DOMContentLoaded') domContentLoaded = () => fn({})
    },
    setTimeout: (fn: () => void, ms?: number) => {
      timers.push(fn)
      delays.push(Number(ms))
      return timers.length
    },
    location: {
      reload: () => {
        reloads++
      },
    },
    ...(opts.storage ? { sessionStorage: opts.storage } : {}),
    setInterval: (fn: () => void) => {
      intervals.push({ fn, live: true })
      return intervals.length
    },
    clearInterval: (id: number) => {
      const slot = intervals[id - 1]
      if (slot) slot.live = false
    },
    MutationObserver: class {
      constructor(fn: () => void) {
        this.entry = { fn, target: null as unknown, live: true }
      }
      entry: { fn: () => void; target: unknown; live: boolean }
      observe(target: unknown) {
        this.entry.target = target
        observers.push(this.entry)
      }
      disconnect() {
        this.entry.live = false
      }
    },
    parent: {
      postMessage: (msg: unknown) => {
        posted.push(msg)
      },
    },
  }
  if (opts.embedded) win.top = {} as object
  else win.top = win.self

  const document = {
    getElementById: (id: string) => nodes[id] ?? null,
    title: opts.title,
  }
  const console = { error: () => {} }

  new Function('window', 'document', 'console', bootScript())(win, document, console)

  return {
    nodes,
    posted,
    /** How many times the shell reloaded this page by itself. */
    reloads: () => reloads,
    /** The delay of every timer the shell armed, in the order it armed them. */
    delays: () => [...delays],
    /** The parser finished and the deferred entry has run or failed. */
    domContentLoaded() {
      expect(domContentLoaded).not.toBeNull()
      domContentLoaded!()
    },
    /** Live watchers, so a test can assert the reveal stopped polling. */
    liveWatchers: () =>
      intervals.filter((i) => i.live).length + observers.filter((o) => o.live).length,
    /** Fire a failed module-script error, then run whatever it scheduled. */
    failModule(src = '/assets/main-abc123.js') {
      expect(listener).not.toBeNull()
      listener!({ target: { tagName: 'SCRIPT', type: 'module', src } })
      while (timers.length > 0) timers.shift()!()
    },
    /**
     * Fire the shape a module that LOADED and then threw produces: a plain error
     * event whose target is the window, carrying no element and no URL.
     */
    throwDuringEval(event: Record<string, unknown> = {}) {
      expect(listener).not.toBeNull()
      listener!({ target: win, message: 'e is not a constructor', ...event })
      while (timers.length > 0) timers.shift()!()
    },
    /**
     * Run the body's retry wiring against this same window, click the button,
     * and report what it fetched before it reloaded.
     */
    async clickRetry() {
      const fetched: Array<{ url: string; init: Record<string, unknown> | undefined }> = []
      const events: string[] = []
      let bodyDone: () => void = () => {}
      let onClick: (() => void) | null = null
      const button = Object.assign(nodes['boot-failure-retry'], {
        disabled: false,
        addEventListener: (_kind: string, fn: () => void) => {
          onClick = fn
        },
      })
      const doc = { getElementById: (id: string) => (id === 'boot-failure-retry' ? button : null) }
      const retryWin = Object.assign(win, {
        location: { href: 'https://host.example/', origin: 'https://host.example' },
        fetch: (url: string, init?: Record<string, unknown>) => {
          fetched.push({ url, init })
          events.push('fetch')
          // Headers first, body later -- as a real fetch settles.
          return Promise.resolve({
            ok: true,
            arrayBuffer: () =>
              new Promise((resolve) => {
                bodyDone = () => {
                  events.push('body')
                  resolve(new ArrayBuffer(0))
                }
              }),
          })
        },
      })
      const location = {
        href: 'https://host.example/',
        origin: 'https://host.example',
        reload: () => {
          events.push('reload')
        },
      }
      new Function('window', 'document', 'navigator', 'location', retryScript())(
        retryWin,
        doc,
        {},
        location,
      )
      expect(onClick).not.toBeNull()
      onClick!()
      for (let i = 0; i < 20; i++) await Promise.resolve()
      const label = button.textContent
      const beforeBody = [...events]
      bodyDone()
      for (let i = 0; i < 20; i++) await Promise.resolve()
      return { fetched, events, beforeBody, label }
    },
    /** Whether the shell registered an unhandledrejection listener at all. */
    hasRejectionListener: () => rejectionListener !== null,
    /** The app finally mounts, and every live watcher gets a chance to notice. */
    mountLate() {
      nodes['root'].firstChild = {}
      for (const o of observers) if (o.live) o.fn()
      for (const i of intervals) if (i.live) i.fn()
    },
    fireNonModule() {
      listener!({ target: { tagName: 'IMG', type: '', src: '/logo.png' } })
      while (timers.length > 0) timers.shift()!()
    },
  }
}

describe('the shell reveals a failure panel when the bundle never runs', () => {
  it('never declares display inline, or hidden would be inert', () => {
    // The one assertion that catches a panel covering a WORKING app. `hidden`
    // renders through the UA stylesheet's plain `[hidden] { display: none }`,
    // which carries no `!important`, so any inline `display` here outranks it:
    // the panel then paints on every load, full viewport at maximum z-index, and
    // being topmost it swallows every click. Verified in Chromium.
    //
    // This asserts the DECLARATION, not a computed value, on purpose: happy-dom
    // does not model `[hidden]` at all -- a plain hidden node still computes
    // `display: block` there -- so `getComputedStyle` cannot tell the two states
    // apart in this environment and would pass either way.
    const tag = panelTag()
    expect(tag).toContain('hidden')
    expect(tag).not.toMatch(/display\s*:/)
  })

  it('shows the panel when a module script fails and #root is still empty', () => {
    const h = harness({ rootHasChildren: false, embedded: false })
    expect(h.nodes['boot-failure'].hidden).toBe(true)
    expect(h.nodes['boot-failure'].style.display).toBe('')
    h.failModule()
    expect(h.nodes['boot-failure'].hidden).toBe(false)
    // Unhiding has to supply display, since the tag deliberately omits it.
    expect(h.nodes['boot-failure'].style.display).toBe('flex')
  })

  it('names the module that failed, so the report is actionable', () => {
    const h = harness({ rootHasChildren: false, embedded: false })
    h.failModule('/assets/vendor-react-XYZ.js')
    // Prefixed: a reader shown only the bare URL did not know what it was for.
    expect(h.nodes['boot-failure-detail'].textContent).toBe(
      'Could not load: /assets/vendor-react-XYZ.js'
    )
    // The fetch path keeps the shipped closing sentence: a retry is the likely cure
    // there, so reporting stays the fallback rather than the headline.
    expect(h.nodes['boot-failure-next-fetch'].hidden).toBe(false)
    expect(h.nodes['boot-failure-next-build'].hidden).toBe(true)
  })

  it('shows the panel when a module loads and then throws while evaluating', () => {
    // The resource arrived, so there is no resource error and no URL -- this is
    // the shape a static chunk-import cycle produces, and the shape the CI cycle
    // gate cannot help with once a build already has one. Without this branch the
    // page is dead and the panel never appears.
    const h = harness({ rootHasChildren: false, embedded: false })
    h.throwDuringEval()
    expect(h.nodes['boot-failure'].hidden).toBe(false)
    expect(h.nodes['boot-failure'].style.display).toBe('flex')
    // The thrown message is the one specific fact a reporter can paste, so it is
    // the detail line rather than a hand-written stand-in.
    expect(h.nodes['boot-failure-detail'].textContent).toBe(
      'Startup error: e is not a constructor'
    )
    // And the explanation swaps with it: leaving "part of its code never arrived"
    // there made the page contradict its own detail line.
    expect(h.nodes['boot-failure-why'].textContent).toContain('arrived but failed while starting')
    // The closing sentence swaps too, or the page asserts a build fault and then
    // hedges about the only button it offers.
    expect(h.nodes['boot-failure-next-fetch'].hidden).toBe(true)
    expect(h.nodes['boot-failure-next-build'].hidden).toBe(false)
  })

  it('names the script a startup parse error came from', () => {
    // "Invalid or unexpected token" alone does not say WHICH of a few hundred
    // chunks failed to parse, and that is the one fact that tells a truncated
    // download apart from a broken build. The engine reports it on the event.
    const h = harness({ rootHasChildren: false, embedded: false })
    h.throwDuringEval({
      message: 'Uncaught SyntaxError: Invalid or unexpected token',
      filename: 'https://host.example/assets/App-AbC123.js?v=1',
      lineno: 1,
      colno: 20481,
    })
    expect(h.nodes['boot-failure-detail'].textContent).toBe(
      'Startup error: Uncaught SyntaxError: Invalid or unexpected token' +
        ' (in /assets/App-AbC123.js:1:20481)'
    )
  })

  it('refetches the script that failed past the HTTP cache before it reloads', async () => {
    // Hashed assets are served `immutable` for a year, so a chunk that arrived
    // cut short is replayed from the HTTP cache on every reload. Dropping the
    // service worker and CacheStorage does not touch that cache; a
    // `cache: 'reload'` fetch of the same URL is what replaces the entry.
    const h = harness({ rootHasChildren: false, embedded: false })
    h.throwDuringEval({
      message: 'Uncaught SyntaxError: Invalid or unexpected token',
      filename: 'https://host.example/assets/App-AbC123.js',
      lineno: 1,
      colno: 20481,
    })
    const { fetched, events, beforeBody, label } = await h.clickRetry()
    expect(fetched).toEqual([
      {
        url: 'https://host.example/assets/App-AbC123.js',
        init: { cache: 'reload', credentials: 'same-origin' },
      },
    ])
    // Not at the headers: a reload then would abort the body mid-transfer and
    // leave the truncated entry in place.
    expect(beforeBody).toEqual(['fetch'])
    expect(events).toEqual(['fetch', 'body', 'reload'])
    expect(label).toBe('Re-downloading the broken file...')
  })

  it('refetches a module that failed to load, too, and never a foreign URL', async () => {
    const h = harness({ rootHasChildren: false, embedded: false })
    h.failModule('https://host.example/assets/vendor-react-XYZ.js')
    h.throwDuringEval({ filename: 'https://cdn.other.example/x.js' })
    const { fetched } = await h.clickRetry()
    expect(fetched.map((f) => f.url)).toEqual(['https://host.example/assets/vendor-react-XYZ.js'])
  })

  it('does not arm on an unhandled rejection, which has no named producer here', () => {
    // Deliberately absent rather than forgotten. The entry is a static module tag,
    // so its fetch failures arrive as `error` events; the only un-awaited pre-mount
    // chain in the entry is DEV-only; the Pierre warm import carries its own catch;
    // a failed lazy chunk surfaces as Vite's `vite:preloadError` event. The one real
    // rejection this shell produced was its own service-worker registration, which
    // is suppressed at source because it says nothing about whether the app booted.
    // Arming on any rejection bought a state no user reaches while costing a copy
    // path that contradicted itself.
    const h = harness({ rootHasChildren: false, embedded: false })
    expect(h.hasRejectionListener()).toBe(false)
  })

  it('leaves a working app alone when it throws after rendering', () => {
    const h = harness({ rootHasChildren: true, embedded: false })
    h.throwDuringEval()
    expect(h.nodes['boot-failure'].hidden).toBe(true)
    expect(h.nodes['boot-failure'].style.display).toBe('')
  })

  it('takes the panel back down when a slow boot mounts late', () => {
    // Both signals the panel arms on are evidence the page MIGHT be dead, never
    // proof: a merely-slow entry chunk and a service-worker retry that lands late
    // look identical to a real failure at signal+4s. If the reveal were one-way,
    // such a boot would render UNDERNEATH a fixed maximum-z-index overlay with no
    // dismiss -- worse than the blank page, because the app is running and
    // unreachable.
    const h = harness({ rootHasChildren: false, embedded: false })
    h.throwDuringEval()
    expect(h.nodes['boot-failure'].hidden).toBe(false)
    h.mountLate()
    expect(h.nodes['boot-failure'].hidden).toBe(true)
    expect(h.nodes['boot-failure'].style.display).toBe('')
  })

  it('stops watching once it has taken the panel back down', () => {
    const h = harness({ rootHasChildren: false, embedded: false })
    h.failModule()
    expect(h.liveWatchers()).toBeGreaterThan(0)
    h.mountLate()
    expect(h.liveWatchers()).toBe(0)
  })

  it('never spells an opening script tag inside an inline script', () => {
    // This has now cost two incidents in this file. An opening script tag inside
    // script content -- even in a comment -- flips the HTML tokenizer into its
    // escaped state, so the next closing tag does not end the element and the
    // remainder is parsed as one module that throws. It is invisible to `vite
    // build` (exit 0) and only `npm run i18n:render` catches it, by rendering the
    // built shell. This makes it a unit failure instead, before the push.
    //
    // Case-insensitive, and both tags tolerate everything the HTML tokenizer does:
    // tag names are case-insensitive, so `<SCRIPT>` flips it exactly the same way,
    // and an end tag closes on any of whitespace, `/` or `>` after the name -- so
    // `</script\t\n bar>` is a real end tag too. `[^>]*` spans newlines, which
    // covers an attribute list broken across lines. CodeQL's js/bad-tag-filter
    // flagged both gaps in earlier versions of this test and was right about the
    // holes, not just the pattern: a guard for this class must not be looser than
    // the parser it is guarding against.
    const html = readFileSync(INDEX_PATH, 'utf8')
    const bodies = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\b[^>]*>/gi)].map(
      (m) => m[1],
    )
    expect(bodies.length).toBeGreaterThan(0)
    for (const body of bodies) expect(body).not.toMatch(/<script\b/i)
  })

  it('gives the copy somewhere to report to, not just advice to report', () => {
    // The copy says a retry that does not help means the build is broken and worth
    // reporting. A reader shown that with no destination said they would not know
    // where -- which makes the detail line above it useless as well. The link is a
    // plain <a> in the shell, so it survives the failed bundle and costs no request.
    const html = readFileSync(INDEX_PATH, 'utf8')
    const panel = html.slice(html.indexOf('<div id="boot-failure"'))
    expect(panel).toMatch(/worth reporting at/)
    expect(panel).toContain('https://github.com/kirodotdev/KiroCrew/issues')
  })

  it('does not let a failed service-worker registration arm the panel', () => {
    // The shell's own `navigator.serviceWorker.register('/sw.js')` is not awaited,
    // so without a `.catch` its rejection IS an unhandledrejection -- and it fails
    // exactly where this matters, on the flaky network the panel exists for. This
    // asserts the source, since the registration lives in the body script and its
    // rejection would reach the head listener as an ordinary one.
    const html = readFileSync(INDEX_PATH, 'utf8')
    const register = html.slice(html.indexOf("serviceWorker.register('/sw.js')"))
    expect(register.slice(0, 60)).toContain('.catch(')
  })
  it('gives the retry control its own focus ring rather than the UA default', () => {
    // The reveal focuses this button, and the UA default ring is not the same
    // everywhere: white on desktop Chromium, ORANGE on mobile, where a reader took
    // the orange to mean "careful" on the one control that is the way out. The rule
    // ships as an inline <style> in the document, which survives the failed CSS
    // chunk exactly as the inline attributes do.
    const html = readFileSync(INDEX_PATH, 'utf8')
    expect(html).toMatch(/#boot-failure-retry:focus\s*\{[^}]*outline/)
  })

  it('shows the panel in an embedded pane whose boot throws, after the longer wait', () => {
    // This path invents no new mc-embedded-boot stage, so it posts nothing. A host
    // that cannot see the failure must not be left showing a blank frame either.
    const h = harness({ rootHasChildren: false, embedded: true })
    h.throwDuringEval()
    expect(h.posted).toEqual([])
    expect(h.delays()).toEqual([8000])
    expect(h.nodes['boot-failure'].hidden).toBe(false)
  })

  it('moves focus to the retry control, so a keyboard user lands on the way out', () => {
    const h = harness({ rootHasChildren: false, embedded: false })
    h.failModule()
    expect(h.nodes['boot-failure-retry'].focused).toBe(true)
  })

  it('stays hidden when the app already rendered', () => {
    // A LAZY chunk failing after boot must not cover a working app: React is
    // mounted, so the app owns that error. This is the check that keeps the
    // panel from becoming a false alarm on every deferred-route hiccup.
    const h = harness({ rootHasChildren: true, embedded: false })
    h.failModule()
    expect(h.nodes['boot-failure'].hidden).toBe(true)
    expect(h.nodes['boot-failure'].style.display).toBe('')
  })

  it('ignores a failure that is not a module script', () => {
    // Load-bearing now that a runtime throw also arms the panel: a resource error
    // carries an element, and an image, font or stylesheet failing costs looks,
    // not the app. Only a module script means the page cannot start.
    const h = harness({ rootHasChildren: false, embedded: false })
    h.fireNonModule()
    expect(h.nodes['boot-failure'].hidden).toBe(true)
    expect(h.nodes['boot-failure'].style.display).toBe('')
  })

  it('tells an embedded host first, then shows the panel if nothing replaced the frame', () => {
    // The Kiro Crew host reloads the frame on script-error, which discards the
    // wait, and keeps an opaque overlay over the frame until ready. A host that
    // ignores the message used to leave a blank frame with no way out.
    const h = harness({ rootHasChildren: false, embedded: true })
    h.failModule()
    expect(h.posted).toEqual([
      { type: 'mc-embedded-boot', v: 1, stage: 'script-error', src: '/assets/main-abc123.js' },
    ])
    expect(h.delays()).toEqual([8000])
    expect(h.nodes['boot-failure'].hidden).toBe(false)
    expect(h.nodes['boot-failure-detail'].textContent).toBe('Could not load: /assets/main-abc123.js')
  })
})

describe('the shell reloads by itself when a boot module fails to load', () => {
  // A boot module that fails to LOAD on a first visit (no service worker controls
  // the page yet, so its retry never runs) leaves the page blank until the panel
  // unless the shell reloads it. One aborted request is all it takes: Chromium
  // aborts every in-flight load with ERR_NETWORK_CHANGED when a host network
  // interface comes or goes.
  // Measured in Chromium, the failed fetch stays in the document's module map, so
  // only a new document recovers it -- hence a reload, bounded so it never loops.
  afterEach(() => {
    vi.restoreAllMocks()
  })

  /** Simulate one page load of a tab whose boot module fails to load. */
  function failingLoad(storage: FakeStorage) {
    const h = harness({ rootHasChildren: false, embedded: false, storage })
    h.failModule()
    h.domContentLoaded()
    return h
  }

  it('reloads after a short delay, before the panel could show', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const storage = memoryStorage()
    const h = harness({ rootHasChildren: false, embedded: false, storage })
    h.failModule()
    expect(h.reloads()).toBe(1)
    // The reload is armed first and fires well inside the panel's 4 s wait, so it
    // replaces the page before the panel is revealed. The panel stays armed as the
    // fallback for a reload that never gets an answer.
    const [reloadMs, revealMs] = h.delays()
    expect(revealMs).toBe(4000)
    expect(reloadMs).toBeGreaterThan(0)
    expect(reloadMs).toBeLessThan(revealMs)
  })

  it('stops after three reloads and ends on the panel', () => {
    const storage = memoryStorage()
    const loads = [1, 2, 3, 4, 5].map(() => failingLoad(storage))
    expect(loads.map((h) => h.reloads())).toEqual([1, 1, 1, 0, 0])
    const last = loads[loads.length - 1]
    expect(last.nodes['boot-failure'].hidden).toBe(false)
    expect(last.nodes['boot-failure-detail'].textContent).toBe('Could not load: /assets/main-abc123.js')
  })

  it('backs off between attempts, every delay jittered and shorter than the panel wait', () => {
    for (const random of [0, 0.999]) {
      vi.spyOn(Math, 'random').mockReturnValue(random)
      const storage = memoryStorage()
      const waits = [1, 2, 3].map(() => failingLoad(storage).delays()[0])
      expect(waits[0]).toBeLessThan(waits[1])
      expect(waits[1]).toBeLessThan(waits[2])
      expect(waits[2]).toBeLessThan(4000)
      vi.restoreAllMocks()
    }
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const low = failingLoad(memoryStorage()).delays()[0]
    vi.spyOn(Math, 'random').mockReturnValue(0.999)
    const high = failingLoad(memoryStorage()).delays()[0]
    expect(high).toBeGreaterThan(low)
  })

  it('does not count down a load that failed when DOMContentLoaded follows', () => {
    // DOMContentLoaded fires after the deferred entry has failed too, so clearing
    // the count there unconditionally would hand every load a fresh budget: a loop.
    const storage = memoryStorage()
    failingLoad(storage)
    expect([...storage.data.values()]).toEqual(['1'])
  })

  it('gives the whole budget back once a load starts', () => {
    const storage = memoryStorage()
    failingLoad(storage)
    failingLoad(storage)
    const healthy = harness({ rootHasChildren: false, embedded: false, storage })
    healthy.domContentLoaded()
    expect(storage.data.size).toBe(0)
    const later = [1, 2, 3, 4].map(() => failingLoad(storage).reloads())
    expect(later).toEqual([1, 1, 1, 0])
  })

  it('never reloads when it cannot keep count, so it can never loop', () => {
    const blocked: FakeStorage = {
      getItem: () => {
        throw new Error('SecurityError')
      },
      setItem: () => {
        throw new Error('SecurityError')
      },
      removeItem: () => {
        throw new Error('SecurityError')
      },
    }
    // Storage that accepts a write and keeps nothing (a full or partitioned store).
    const forgetful: FakeStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }
    for (const storage of [blocked, forgetful]) {
      const h = failingLoad(storage)
      expect(h.reloads()).toBe(0)
      expect(h.nodes['boot-failure'].hidden).toBe(false)
    }
    // And with no sessionStorage at all.
    const bare = harness({ rootHasChildren: false, embedded: false })
    bare.failModule()
    expect(bare.reloads()).toBe(0)
  })

  it('reloads once per document however many boot modules failed in it', () => {
    const storage = memoryStorage()
    const h = harness({ rootHasChildren: false, embedded: false, storage })
    h.failModule('/assets/main-abc123.js')
    h.failModule('/assets/vendor-react-XYZ.js')
    expect(h.reloads()).toBe(1)
    expect([...storage.data.values()]).toEqual(['1'])
  })

  it('leaves a page that already started alone', () => {
    // A module tag that fails after DOMContentLoaded belongs to a running page;
    // reloading it would throw away whatever the person was doing.
    const storage = memoryStorage()
    const h = harness({ rootHasChildren: false, embedded: false, storage })
    h.domContentLoaded()
    h.failModule()
    expect(h.reloads()).toBe(0)
    expect(storage.data.size).toBe(0)
  })

  it('never reloads an embedded pane, which keeps its mc-embedded-boot contract', () => {
    const storage = memoryStorage()
    const h = harness({ rootHasChildren: false, embedded: true, storage })
    h.failModule()
    expect(h.reloads()).toBe(0)
    expect(storage.data.size).toBe(0)
    expect(h.posted).toEqual([
      { type: 'mc-embedded-boot', v: 1, stage: 'script-error', src: '/assets/main-abc123.js' },
    ])
  })

  it('never reloads for a module that loaded and then threw', () => {
    // An evaluation throw is the build, not the network: a reload replays it.
    const storage = memoryStorage()
    const h = harness({ rootHasChildren: false, embedded: false, storage })
    h.throwDuringEval()
    expect(h.reloads()).toBe(0)
    expect(h.nodes['boot-failure'].hidden).toBe(false)
  })

  it('gives the reload the panel button starts the whole budget back', async () => {
    // The button is the person asking for a reload. Left at the spent count, the
    // load it starts would get no automatic reload at all, unlike a first visit.
    const storage = memoryStorage()
    const loads = [1, 2, 3, 4].map(() => failingLoad(storage))
    expect([...storage.data.values()]).toEqual(['3'])
    const { events } = await loads[loads.length - 1].clickRetry()
    expect(events).toContain('reload')
    expect(storage.data.size).toBe(0)
  })

  it('says plainly that Kiro Crew did not load and offers a reload', () => {
    const html = readFileSync(INDEX_PATH, 'utf8')
    const panel = html.slice(html.indexOf('<div id="boot-failure"'))
    const heading = panel.slice(panel.indexOf('<h1'), panel.indexOf('</h1>'))
    expect(heading.slice(heading.indexOf('>') + 1)).toBe('Kiro Crew did not load')
    const button = panel.slice(panel.indexOf('<button id="boot-failure-retry"'), panel.indexOf('</button>'))
    expect(button.slice(button.lastIndexOf('>') + 1)).toBe('Clear cache and reload')
  })

  it('names the product from the shell title, which an edition rebrands', () => {
    const h = harness({ rootHasChildren: false, embedded: false, title: 'Acme Crew' })
    h.failModule()
    expect(h.nodes['boot-failure-title'].textContent).toBe('Acme Crew did not load')
  })
})
