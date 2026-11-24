/**
 * Colours for the XR scene.
 *
 * The glasses have an additive display: black is see-through and a light
 * surface glows over the room. So a panel cannot take the theme's background.
 * Panels are near-black glass with light text in every theme, and only the
 * meaning colours (accent, warning, danger, success) come from the theme's
 * tokens. The numbers below are GL colours; `css()` spells one for canvas 2D.
 */
export const XR_GL = {
  /** Clear colour of the desktop preview. In XR the frame is transparent. */
  room: 0x07090d,
  glass: 0x0b0e14,
  ink: 0xf3f5f9,
  dim: 0xa9b1c0,
  faint: 0x5c6575,
  edge: 0x2a3140,
  raised: 0x171c26,
  white: 0xffffff,
} as const

export const css = (gl: number): string => '#' + gl.toString(16).padStart(6, '0')

export interface XrPalette {
  glass: string
  ink: string
  dim: string
  faint: string
  edge: string
  raised: string
  accent: string
  warn: string
  danger: string
  ok: string
  sans: string
  mono: string
}

/** Read the theme's meaning colours and fonts through a probe element. */
export function readPalette(host: HTMLElement): XrPalette {
  const probe = document.createElement('span')
  probe.style.display = 'none'
  host.appendChild(probe)
  const token = (name: string, fallback: string): string => {
    // An unset token would resolve to the inherited text colour, so ask for the token itself first.
    if (!getComputedStyle(host).getPropertyValue(name).trim()) return fallback
    probe.style.color = `var(${name})`
    return getComputedStyle(probe).color || fallback
  }
  const ink = css(XR_GL.ink)
  const pal: XrPalette = {
    glass: css(XR_GL.glass),
    ink,
    dim: css(XR_GL.dim),
    faint: css(XR_GL.faint),
    edge: css(XR_GL.edge),
    raised: css(XR_GL.raised),
    accent: token('--accent', ink),
    warn: token('--warn', ink),
    danger: token('--danger', ink),
    ok: token('--ok', ink),
    sans: getComputedStyle(document.body).fontFamily || 'sans-serif',
    mono: getComputedStyle(host).getPropertyValue('--theme-font-mono').trim() || 'monospace',
  }
  probe.remove()
  return pal
}
