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

/** The panel's opening tag, straight out of the shell markup. */
function panelTag(): string {
  const html = readFileSync(INDEX_PATH, 'utf8')
  const start = html.indexOf('<div id="boot-failure"')
  expect(start).toBeGreaterThan(-1)
  const end = html.indexOf('>', start)
  expect(end).toBeGreaterThan(start)
  return html.slice(start, end + 1)
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

/** A DOM stub with just the nodes the panel logic touches. */
function harness(opts: { rootHasChildren: boolean; embedded: boolean }) {
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
    'boot-failure-why': make('boot-failure-why'),
    'boot-failure-next-fetch': make('boot-failure-next-fetch'),
    'boot-failure-next-build': make('boot-failure-next-build', null, true),
    'boot-failure-detail': make('boot-failure-detail'),
    'boot-failure-retry': make('boot-failure-retry'),
  }
  let listener: ((e: unknown) => void) | null = null
  let rejectionListener: ((e: unknown) => void) | null = null
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
    },
    setTimeout: (fn: () => void) => {
      timers.push(fn)
      return timers.length
    },
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
  }
  const console = { error: () => {} }

  new Function('window', 'document', 'console', bootScript())(win, document, console)

  return {
    nodes,
    posted,
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
    throwDuringEval() {
      expect(listener).not.toBeNull()
      listener!({ target: win, message: 'e is not a constructor' })
      while (timers.length > 0) timers.shift()!()
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

  it('does not paint over an embedded pane when its boot throws', () => {
    // The host is listening for mc-embedded-boot; this path invents no new stage,
    // so it must stay silent rather than post one.
    const h = harness({ rootHasChildren: false, embedded: true })
    h.throwDuringEval()
    expect(h.nodes['boot-failure'].hidden).toBe(true)
    expect(h.posted).toEqual([])
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

  it('leaves an embedded pane to its host instead of covering the frame', () => {
    // An embedded pane has a parent listening for mc-embedded-boot; painting a
    // full-screen panel inside someone else's frame would hide their own error UI.
    const h = harness({ rootHasChildren: false, embedded: true })
    h.failModule()
    expect(h.nodes['boot-failure'].hidden).toBe(true)
    expect(h.nodes['boot-failure'].style.display).toBe('')
    expect(h.posted).toEqual([
      { type: 'mc-embedded-boot', v: 1, stage: 'script-error', src: '/assets/main-abc123.js' },
    ])
  })
})
