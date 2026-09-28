/**
 * Minimal CSS Custom Highlight API for happy-dom, which ships `Range` but neither
 * `Highlight` nor `CSS.highlights`. `Highlight` is a Set of Ranges plus the
 * `priority` / `type` attributes; the registry is a Map keyed by highlight name.
 * Painting is not modelled — tests read the registered ranges back and compare
 * their text, which is what the real API paints.
 */
export class HighlightStub extends Set<Range> {
  priority = 0
  type: 'highlight' | 'spelling-error' | 'grammar-error' = 'highlight'
}

export function installHighlightApiStub(): void {
  const g = globalThis as unknown as { Highlight?: unknown; CSS?: object }
  g.Highlight = HighlightStub
  // happy-dom exposes `CSS` as a getter that builds a fresh object on every
  // read, so a property set on one read is gone on the next. Pin one object
  // (inheriting the original's statics such as `escape`) and hang the registry
  // on it, so `CSS.highlights` reads back exactly as the browser exposes it.
  const current = g.CSS as { highlights?: unknown } | undefined
  if (!current?.highlights) {
    const pinned = Object.create(current ?? null) as { highlights?: unknown }
    Object.defineProperty(pinned, 'highlights', {
      value: new Map<string, HighlightStub>(),
      configurable: true,
      writable: true,
    })
    Object.defineProperty(g, 'CSS', { value: pinned, configurable: true, writable: true })
  }
}

/** Ranges registered under `name`, or an empty list when nothing is painted. */
export function registeredRanges(name: string): Range[] {
  const registry = (globalThis as unknown as { CSS?: { highlights?: Map<string, HighlightStub> } }).CSS?.highlights
  const hl = registry?.get(name)
  return hl ? Array.from(hl) : []
}

/** The text each registered range paints, in registration order. */
export function paintedText(name: string): string[] {
  return registeredRanges(name).map(r => r.toString())
}
