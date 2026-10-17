/**
 * Favicon legibility: decide when a site icon needs a contrasting plate behind
 * it, and pick nothing else — the caller owns the styling.
 *
 * A favicon is an arbitrary transparent image drawn straight onto a themed
 * surface, so nothing guarantees the two are distinguishable. The common
 * failure is a site that ships ONE icon designed for a white browser tab
 * (GitHub's octocat is a near-black glyph on transparency): on a dark theme it
 * is a black shape on a black background — present in the DOM, invisible to the
 * reader.
 *
 * Choosing per THEME would be a coin flip, because the icon's own colours are
 * what decide it: a dark theme is only a problem for dark icons, and a light
 * theme only for light ones. Choosing per SITE (a hardcoded list, or honouring
 * `<link media="(prefers-color-scheme: dark)">`) fixes only the sites that
 * declare a variant — GitHub, the site that motivates this, swaps its favicon
 * from JavaScript and declares no media variant at all, so a markup-driven fix
 * never reaches it.
 *
 * What is left is to look at the pixels: sample the decoded icon, sample the
 * surface it is painted on, and plate only the pair that actually collides.
 * Every icon arrives as a `data:` URI (see `lib/linkMeta.ts`), so the sampling
 * canvas is same-origin and never tainted.
 */

/** sRGB channel (0-255) → linear-light, per the WCAG relative-luminance definition. */
function linearize(channel: number): number {
  const s = channel / 255
  return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4)
}

/** WCAG relative luminance (0 = black, 1 = white) of an sRGB triple. */
export function relativeLuminance(r: number, g: number, b: number): number {
  return 0.2126 * linearize(r) + 0.7152 * linearize(g) + 0.0722 * linearize(b)
}

/** WCAG contrast ratio (1–21) between two relative luminances. */
export function contrastRatio(a: number, b: number): number {
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

/**
 * Contrast below which an icon is treated as lost in its surface.
 *
 * Deliberately far under WCAG's 3:1 for meaningful non-text content. This is a
 * repair, not a compliance floor, and it can only ever paint over what the site
 * shipped — so it aims at the band where an icon is not dim but absent, and
 * concedes the merely washy ones. At 3:1 a mid-tone brand colour that reads
 * perfectly well (a blue, an orange, a saturated green on a light theme, all
 * landing between 1.9 and 4) also trips the test and earns a plate it does not
 * need, making the fix more visible than the bug.
 *
 * 1.6 was set by measuring real favicons at both ends. The cases that actually
 * disappear sit near the bottom of the scale — GitHub's light-mode octocat
 * measures 1.02 against the dark theme's background, and the white octocat it
 * serves to dark clients measures 1.20 against the light theme's — while
 * Google, Stack Overflow, Wikipedia, Amazon, npm, GitLab and Python all clear
 * the threshold untouched in both modes.
 */
export const MIN_ICON_CONTRAST = 1.6

/**
 * Below this painted fraction there is nothing to rescue — an icon that covers
 * a couple of pixels of its box is not a shape a plate would make readable, and
 * plating it would draw a filled square where the site drew almost nothing.
 */
export const MIN_ICON_COVERAGE = 0.02

/** Alpha under which a pixel counts as unpainted rather than faint. */
const ALPHA_FLOOR = 16

/** Edge length of the sampling canvas. Favicons are 16–180px; the icon is
 *  rendered at ~14–32px anyway, and the average is what matters, not detail. */
const SAMPLE_EDGE = 16

/**
 * The icon's tone, summarised as the two ENDS of its luminance range rather
 * than a single average.
 *
 * A mean alone mislabels an icon that carries its own opaque backdrop — a black
 * tile with white lettering averages to "dark" and would be plated on a dark
 * theme even though its own contrast makes it perfectly readable. Keeping the
 * dim and bright ends lets the decision be "no part of this icon stands out
 * from the surface", which is the condition that actually makes it disappear.
 *
 * Both ends are percentiles, not the true min/max: one stray antialiased pixel
 * should not vouch for the whole icon.
 */
export interface IconTone {
  /** 5th-percentile relative luminance of the painted pixels. */
  dim: number
  /** 95th-percentile relative luminance of the painted pixels. */
  bright: number
  /** Alpha-weighted fraction of the sampled box the icon paints (0–1). */
  coverage: number
}

/** Percentile of an ASCENDING array. */
function percentile(sorted: number[], p: number): number {
  const i = Math.round((sorted.length - 1) * p)
  return sorted[Math.min(sorted.length - 1, Math.max(0, i))]
}

/**
 * Summarise RGBA bytes (canvas `ImageData` order) into an {@link IconTone}, or
 * `null` when nothing is painted.
 */
export function toneFromPixels(pixels: ArrayLike<number>): IconTone | null {
  const total = Math.floor(pixels.length / 4)
  if (total === 0) return null
  const lums: number[] = []
  let painted = 0
  for (let i = 0; i < total * 4; i += 4) {
    const alpha = pixels[i + 3]
    if (alpha < ALPHA_FLOOR) continue
    painted += alpha / 255
    lums.push(relativeLuminance(pixels[i], pixels[i + 1], pixels[i + 2]))
  }
  if (lums.length === 0) return null
  lums.sort((a, b) => a - b)
  return {
    dim: percentile(lums, 0.05),
    bright: percentile(lums, 0.95),
    coverage: painted / total,
  }
}

/** One reused canvas: a transcript can hold dozens of chips, and each would
 *  otherwise allocate (and leak, until GC) a backing store of its own. */
let sampler: HTMLCanvasElement | null = null

/**
 * Sample a decoded `<img>`, or `null` when it cannot be read — not yet decoded,
 * zero-sized, or an environment with no 2D canvas (jsdom without the `canvas`
 * package, which is how the unit tests run). Every `null` path means "no
 * opinion", and the caller must leave the icon exactly as the site shipped it.
 */
export function measureIconTone(img: HTMLImageElement): IconTone | null {
  if (!img.naturalWidth || !img.naturalHeight) return null
  try {
    if (!sampler) sampler = document.createElement('canvas')
    sampler.width = SAMPLE_EDGE
    sampler.height = SAMPLE_EDGE
    const ctx = sampler.getContext('2d', { willReadFrequently: true })
    if (!ctx) return null
    ctx.clearRect(0, 0, SAMPLE_EDGE, SAMPLE_EDGE)
    ctx.drawImage(img, 0, 0, SAMPLE_EDGE, SAMPLE_EDGE)
    return toneFromPixels(ctx.getImageData(0, 0, SAMPLE_EDGE, SAMPLE_EDGE).data)
  } catch {
    return null
  }
}

interface Rgba {
  r: number
  g: number
  b: number
  a: number
}

const FUNCTIONAL_RGB = /^rgba?\(([^)]*)\)$/i
/** `color(srgb r g b / a)` — how a browser serialises the `color-mix()` behind
 *  the theme's alpha utilities (`bg-accent/10`). */
const COLOR_SRGB = /^color\(\s*srgb\s+([^)]*)\)$/i
const HEX = /^#([0-9a-f]{3,8})$/i

function numbers(body: string): number[] {
  return body
    .split(/[\s,/]+/)
    .filter((p) => p.length > 0)
    .map(Number)
}

/**
 * Parse the colour forms that reach us: computed `backgroundColor` (always
 * functional notation) and raw custom-property values (hex, as the themes in
 * `index.css` write them). Anything else — a gradient, a colour space we do not
 * recognise — returns `null`, which callers read as "keep looking", never as a
 * colour.
 */
export function parseCssColor(value: string): Rgba | null {
  const v = value.trim().toLowerCase()
  if (!v) return null
  if (v === 'transparent') return { r: 0, g: 0, b: 0, a: 0 }

  const fn = FUNCTIONAL_RGB.exec(v)
  if (fn) {
    const n = numbers(fn[1])
    if (n.length < 3 || n.some((x) => !Number.isFinite(x))) return null
    return { r: n[0], g: n[1], b: n[2], a: n.length > 3 ? n[3] : 1 }
  }

  const srgb = COLOR_SRGB.exec(v)
  if (srgb) {
    const n = numbers(srgb[1])
    if (n.length < 3 || n.some((x) => !Number.isFinite(x))) return null
    // color() channels are 0–1.
    return { r: n[0] * 255, g: n[1] * 255, b: n[2] * 255, a: n.length > 3 ? n[3] : 1 }
  }

  const hex = HEX.exec(v)
  if (hex) {
    const h = hex[1]
    const wide = h.length > 4
    const step = wide ? 2 : 1
    const parts: number[] = []
    for (let i = 0; i + step <= h.length; i += step) {
      const chunk = h.slice(i, i + step)
      parts.push(parseInt(wide ? chunk : chunk + chunk, 16))
    }
    if (parts.length < 3 || parts.some((x) => Number.isNaN(x))) return null
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] / 255 : 1 }
  }
  return null
}

/**
 * Relative luminance of what is actually painted behind *el*, or `null` when it
 * cannot be determined.
 *
 * Walks up from *el* compositing every translucent background it passes (the
 * chip's own `bg-accent/10` tint, a hovered row, a card) over the first opaque
 * one. A single `getComputedStyle` on the chip would report only that tint and
 * miss the page underneath, and hardcoding `--bg` would be wrong for a chip
 * inside a card — or under any installed theme.
 *
 * `--bg` is the last resort for the case where no ancestor declares an opaque
 * background at all.
 */
export function surfaceLuminance(el: Element | null): number | null {
  const translucent: Rgba[] = []
  let base: Rgba | null = null
  for (let node: Element | null = el; node; node = node.parentElement) {
    const c = parseCssColor(getComputedStyle(node).backgroundColor)
    if (!c || c.a <= 0) continue
    if (c.a >= 0.999) {
      base = c
      break
    }
    translucent.push(c)
  }
  if (!base) {
    const themeBg = parseCssColor(
      getComputedStyle(document.documentElement).getPropertyValue('--bg'),
    )
    if (!themeBg || themeBg.a < 0.999) return null
    base = themeBg
  }
  let { r, g, b } = base
  // Collected top-down, so composite in reverse: nearest the base first.
  for (let i = translucent.length - 1; i >= 0; i--) {
    const l = translucent[i]
    r = l.r * l.a + r * (1 - l.a)
    g = l.g * l.a + g * (1 - l.a)
    b = l.b * l.a + b * (1 - l.a)
  }
  return relativeLuminance(r, g, b)
}

/**
 * True when *surface* is the kind of background a site's
 * `prefers-color-scheme: dark` icon was drawn for.
 *
 * The midpoint is enough of a line to draw: real themes cluster hard at the
 * extremes (~0.01 for the dark palettes in `index.css`, ~0.95 for the light
 * ones), so nothing meaningful sits near 0.5, and an unmeasurable surface
 * (`null`) must keep the default icon rather than guess.
 */
export function prefersDarkIcon(surface: number | null): boolean {
  return surface !== null && surface <= 0.5
}

/**
 * True when NO part of the icon separates from its surface — neither its dim
 * end nor its bright end clears {@link MIN_ICON_CONTRAST}.
 *
 * Requiring both ends to fail is what keeps the plate off icons that supply
 * their own contrast internally. A missing measurement is never a reason to
 * plate: unknown means untouched.
 */
export function iconNeedsPlate(tone: IconTone | null, surface: number | null): boolean {
  if (!tone || surface === null) return false
  if (tone.coverage < MIN_ICON_COVERAGE) return false
  return (
    contrastRatio(tone.dim, surface) < MIN_ICON_CONTRAST &&
    contrastRatio(tone.bright, surface) < MIN_ICON_CONTRAST
  )
}

const listeners = new Set<() => void>()
let observer: MutationObserver | null = null

/**
 * Notify *listener* whenever the computed theme colours may have changed, so a
 * decision taken from the DOM can be retaken.
 *
 * Watching `documentElement` covers all three ways the palette moves: the
 * `data-theme` attribute (mode and colour-theme switches), an inline `style`
 * carrying a custom theme's variables, and a `class` change. One observer is
 * shared by every subscriber and torn down with the last of them, because the
 * subscribers are per-icon and a transcript can hold many.
 */
export function subscribeThemeSurface(listener: () => void): () => void {
  listeners.add(listener)
  if (!observer && typeof MutationObserver !== 'undefined') {
    observer = new MutationObserver(() => {
      for (const l of listeners) l()
    })
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme', 'style', 'class'],
    })
  }
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0 && observer) {
      observer.disconnect()
      observer = null
    }
  }
}
