/**
 * KaTeX on demand. rehype-katex, KaTeX (258,440 B minified, about 252 KB) and
 * its stylesheet (29,736 B) load the first time a parsed block holds math, not
 * with the first paint. Until they land, math shows as its TeX source in the code element
 * remark-math emits; then every block whose text may hold math parses again.
 *
 * A failed load never reloads the page (main.tsx cancels its preload error via
 * `isKatexChunkError`): math stays TeX source, and the next block that holds
 * math tries again. A failed stylesheet does load on that retry.
 * shortcut: after a failed KaTeX script load, math stays as TeX source until
 * the next page load. Chromium keeps a failed module fetch in its module map
 * for the life of the page, so the retry fails at once with no request (the
 * same limit appRegistryPrefetch.ts accepts). Upgrade when Chromium ships
 * whatwg/html#10327, which stops caching those failures.
 */
import type { Root, Element, ElementContent } from 'hast'

type Transform = (tree: Root, file: unknown) => void

let transform: Transform | null = null
let loading: Promise<void> | null = null
/** True from the `import()` until it settles; read by `isKatexChunkError`. */
let inFlight = false
/** The stylesheet URL `isKatexChunkError` saw fail; the script may still load. */
let failedCss: string | null = null
/** A stylesheet that failed before. Vite never preloads a URL twice, so the
 *  next attempt adds its <link> itself. */
let retryCss: string | null = null
let failedAt = 0
let generation = 0
const listeners = new Set<() => void>()
/** After a failure, a new attempt waits this long, so a streaming message does
 *  not fire one request per parse while the network is down. */
const RETRY_AFTER_MS = 2000
const KATEX_CHUNK_RE = /\/assets\/katex(?:Plugin)?-[^/\s]*\.(?:js|css)\b/

export function katexGeneration(): number {
  return generation
}

export function subscribeKatex(cb: () => void): () => void {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

function addStylesheet(href: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = href
    link.onload = () => resolve()
    link.onerror = () => { link.remove(); reject(new Error(`Unable to load ${href}`)) }
    document.head.appendChild(link)
  })
}

export function loadKatex(): Promise<void> {
  if (loading) return loading
  inFlight = true
  const css = retryCss
  retryCss = null
  // Handlers go on a separate statement: chained on `import()` itself, Vite's
  // build runs them inside its preload wrapper, before `vite:preloadError`
  // fires (same as appRegistryPrefetch.ts).
  const p = import('./katexPlugin')
  // Both settle before the outcome is read, so a failed sheet is never lost.
  const script = p.then((mod) => mod, () => undefined)
  const sheet = css ? addStylesheet(css).then(() => true, () => false) : true
  loading = Promise.all([script, sheet]).then(([mod, sheetOk]) => {
    if (!sheetOk) failedCss = css
    inFlight = false
    // main.tsx cancels the preload error; Vite then resolves to undefined.
    if (!mod || failedCss) {
      loading = null
      failedAt = Date.now()
      retryCss = failedCss
      failedCss = null
      throw new Error('KaTeX chunk failed to load')
    }
    transform = mod.default() as Transform
    generation += 1
    for (const cb of listeners) cb()
  })
  return loading
}

/**
 * Whether a `vite:preloadError` is the KaTeX chunk or its stylesheet. main.tsx
 * then cancels it instead of reloading: math falls back to TeX source.
 */
export function isKatexChunkError(error: unknown): boolean {
  if (!inFlight) return false
  const message = error && typeof error === 'object' && 'message' in error ? (error as { message?: unknown }).message : undefined
  if (typeof message !== 'string') return false
  // WebKit names no URL; in this window the KaTeX load is the likely source.
  const ours = !/\/assets\//.test(message) || KATEX_CHUNK_RE.test(message)
  // A failed stylesheet still lets the script load; count it as a failure.
  if (ours) failedCss = /^Unable to preload CSS for (\S+)$/.exec(message)?.[1] ?? failedCss
  return ours
}

/**
 * Cheap text test: false only when the text cannot hold math rehype-katex
 * renders. Math comes from remark-math's `$$`, remarkLatexDelimiters' `\\(` and
 * `\\[`, a ```math fence (`language-math`), or raw HTML carrying one of
 * `MATH_CLASSES`; the last two both spell `math`. Prose that says "math" only
 * costs one extra parse when KaTeX lands.
 */
export function mayHoldMath(text: string): boolean {
  return text.includes('$$') || text.includes('\\(') || text.includes('\\[') || text.includes('math')
}

const MATH_CLASSES = ['language-math', 'math-display', 'math-inline']

function hasMath(nodes: ElementContent[] | Root['children']): boolean {
  for (const node of nodes) {
    if (node.type !== 'element') continue
    const el = node as Element
    const cls = el.properties?.className
    if (Array.isArray(cls) && cls.some((c) => MATH_CLASSES.includes(String(c)))) return true
    if (hasMath(el.children)) return true
  }
  return false
}

/** rehype plugin: KaTeX once loaded; until then, start the load when the tree has math. */
export function rehypeKatexLazy() {
  return (tree: Root, file: unknown) => {
    if (transform) return transform(tree, file)
    if (!hasMath(tree.children)) return
    if (!loading && Date.now() - failedAt < RETRY_AFTER_MS) return
    loadKatex().catch(() => {})
  }
}
