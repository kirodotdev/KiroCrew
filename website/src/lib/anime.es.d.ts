// Minimal ambient types for the vendored anime.js v3.2.2 ES module
// (src/lib/anime.es.js). anime.js ships its own richer types when installed
// from npm; this shim covers only the surface we use until the dependency is
// added as a public npm dependency.
//
// Note: anime.js 3.2.2's published anime.es.js sets `anime.version = '3.2.1'`
// internally -- an upstream quirk (the constant was never bumped for the 3.2.2
// release). The vendored file is the genuine 3.2.2 artifact from jsdelivr
// (cdn.jsdelivr.net/npm/animejs@3.2.2/lib/anime.es.js).
type AnimeTarget =
  | string
  | object
  | HTMLElement
  | SVGElement
  | NodeListOf<Element>
  | ArrayLike<Element>
  | null

type AnimeValue = number | string
type AnimePropFn = (el: Element, i: number, total: number) => AnimeValue

interface AnimeInstance {
  play(): void
  pause(): void
  restart(): void
  seek(time: number): void
  finished: Promise<void>
  [key: string]: unknown
}

interface AnimeTimelineInstance extends AnimeInstance {
  add(params: Record<string, unknown>, offset?: string | number): AnimeTimelineInstance
}

interface AnimeStatic {
  (params: Record<string, unknown>): AnimeInstance
  timeline(params?: Record<string, unknown>): AnimeTimelineInstance
  stagger(
    value: number | string | [number, number],
    options?: Record<string, unknown>,
  ): AnimePropFn
  random(min: number, max: number): number
  set(targets: AnimeTarget, params: Record<string, unknown>): void
  remove(targets: AnimeTarget): void
  get(targets: AnimeTarget, prop: string): string | number
  [key: string]: unknown
}

declare const anime: AnimeStatic
export default anime
