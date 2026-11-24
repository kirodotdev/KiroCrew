/**
 * A page source that shows STILL pictures of a page: the same page, taken at a few sizes.
 *
 * It is test content. The pictures are handed in by address through the test hook of `?view=pages`; none ships
 * with the page. A still cannot be laid out again, so asked for a size it settles on the nearest size it was taken
 * at (`source.ts`: the panel takes the size the source gives). A pointer and a scroll change nothing in a picture:
 * they are counted, so a test can see that the panel handed them over, in the page's own pixels.
 */
import { SRGBColorSpace, Texture } from 'three'
import type { PageFrame, PagePointer, PageScroll, PageSource } from './source'

/** One picture of the page: where it is, how big the page was in CSS pixels, and how many picture pixels a CSS pixel has. */
export interface Still { url: string; cssWidth: number; cssHeight: number; dpr: number }

/** Only a picture this page's own origin serves is loaded: nothing is asked of another origin. */
function sameOrigin(url: string): string {
  const at = new URL(url, window.location.origin)
  if (at.origin !== window.location.origin) throw new Error('a still must come from this origin')
  return at.href
}

export class StillSource implements PageSource {
  /** What the panel handed over, for the test probe. */
  readonly seen = { moves: 0, downs: 0, ups: 0, cancels: 0, leaves: 0, scrolls: 0, scrolledPx: 0, last: null as PagePointer | null }
  private readonly listeners = new Set<() => void>()
  private readonly blank = new Texture()
  private texture: Texture | null = null
  /** The picture before this one. A panel may still draw it for a frame, so it is freed one change later. */
  private before: Texture | null = null
  private now: Still
  /** The picture that was last asked for. It is `now` once it has arrived. */
  private want: Still
  /** Counts the loads, so a picture that arrives late for a size that was left is dropped. */
  private asked = 0
  private gone = false

  constructor(readonly id: string, private readonly stills: readonly Still[], cssWidth: number) {
    if (!stills.length) throw new Error('a still source needs a picture')
    this.now = this.want = this.nearest(cssWidth)
    this.load(this.now)
  }

  /** The picture taken at the width nearest to `cssWidth`, as a ratio: 1100 is nearer to 1024 than to 1280. */
  private nearest(cssWidth: number): Still {
    return this.stills.reduce((best, s) => (Math.abs(Math.log(s.cssWidth / cssWidth)) < Math.abs(Math.log(best.cssWidth / cssWidth)) ? s : best))
  }

  private load(still: Still): void {
    const turn = (this.asked += 1)
    const img = new Image()
    img.decoding = 'async'
    img.onload = () => {
      if (this.gone || turn !== this.asked) return
      const texture = new Texture(img)
      texture.colorSpace = SRGBColorSpace
      texture.needsUpdate = true
      // The picture before stays up until the new one is here, so a page never blinks while its size changes.
      this.before?.dispose()
      this.before = this.texture
      this.texture = texture
      this.now = still
      for (const fn of this.listeners) fn()
    }
    img.src = sameOrigin(still.url)
  }

  /** The picture that is shown, for the test probe: its size in picture pixels, and whether it has arrived. */
  get shown(): { cssWidth: number; cssHeight: number; dpr: number; loaded: boolean } {
    return { cssWidth: this.now.cssWidth, cssHeight: this.now.cssHeight, dpr: this.now.dpr, loaded: !!this.texture }
  }

  frame(): PageFrame { return { texture: this.texture ?? this.blank, cssWidth: this.now.cssWidth, cssHeight: this.now.cssHeight } }

  subscribe(changed: () => void): () => void {
    this.listeners.add(changed)
    return () => { this.listeners.delete(changed) }
  }

  resize(cssWidth: number): void {
    const want = this.nearest(cssWidth)
    if (want === this.want) return
    this.want = want
    this.load(want)
  }

  pointer(event: PagePointer): void {
    const key = ({ move: 'moves', down: 'downs', up: 'ups', cancel: 'cancels', leave: 'leaves' } as const)[event.type]
    this.seen[key] += 1
    this.seen.last = event
  }

  scroll(event: PageScroll): void {
    this.seen.scrolls += 1
    this.seen.scrolledPx += event.dy
  }

  dispose(): void {
    this.gone = true
    this.listeners.clear()
    this.texture?.dispose()
    this.before?.dispose()
    this.blank.dispose()
  }
}
