import type { CSSProperties, SVGProps } from 'react'

/**
 * Hand-rolled panel / sidebar icons — KiroCrew's replacement for lucide's
 * PanelLeft / PanelLeftClose / PanelRight / PanelRightClose / PanelBottom.
 *
 * Each glyph is an outlined frame with the pane drawn as a filled rounded
 * block. State is carried by the pane, never by embedded arrows:
 *   - THIN + solid pane  -> the panel is CLOSED / can be summoned; "view in
 *     panel" affordances use it because they open the panel
 *   - THICK + dimmed pane -> the panel is OPEN; clicking tucks it away
 *
 * Hover morph is OPT-IN: an enclosing control carrying the `pi-morph` class
 * (button / [role="button"] / a) previews its click's outcome on hover — the
 * pane smoothly morphs to the opposite state's geometry and fill. Apply
 * `pi-morph` only where the icon is a live toggle/opener for the panel it
 * depicts; leave it off pure indicators (e.g. SplitGlyph's split-direction
 * glyph), where a state-preview would be false.
 *
 * Geometry has ONE source of truth: PANE_GEOMETRY below. The component emits
 * both the resting and hover-target values as CSS custom properties, and the
 * `.pi-pane` rules in index.css are pure mechanism (they only reference the
 * variables). The rect's SVG attributes carry the resting values too, as the
 * fallback for browsers without SVG geometry-property CSS support.
 *
 * The dimmed fill uses fillOpacity on currentColor, so it tracks the button's
 * text color through hover/theme changes with no extra states.
 *
 * The glyph deliberately fills more of the 24px viewBox than lucide's panel
 * icons (frame 19x17.5 vs 18x15): at the 12-16px sizes these render at, the
 * stock proportions were hard to read.
 *
 * API is lucide-compatible (size + spread SVG props incl. className), so these
 * drop into existing <PanelLeft size={16}/> call sites unchanged.
 */
type PanelIconProps = SVGProps<SVGSVGElement> & { size?: number | string }

const FRAME = { x: 2.5, y: 3.25, width: 19, height: 17.5, rx: 3 } as const

/** Pane rect per side and state — the single source of truth for pane
 *  geometry. The pane hugs its frame edge and grows inward: thin = panel
 *  closed, thick = panel open. */
const PANE_GEOMETRY = {
  left: {
    closed: { x: 4.5, y: 5.25, width: 2.4, height: 13.5, rx: 1.2 },
    open: { x: 4.5, y: 5.25, width: 6.5, height: 13.5, rx: 1.4 },
  },
  right: {
    closed: { x: 17.1, y: 5.25, width: 2.4, height: 13.5, rx: 1.2 },
    open: { x: 13, y: 5.25, width: 6.5, height: 13.5, rx: 1.4 },
  },
  bottom: {
    closed: { x: 4.5, y: 16.35, width: 15, height: 2.4, rx: 1.2 },
    open: { x: 4.5, y: 13.25, width: 15, height: 5.5, rx: 1.4 },
  },
} as const

/** Dimmed "already open" pane fill — low enough to read as not-solid, high
 *  enough to stay legible on dim muted-gray themes. */
const LIGHT_OPACITY = 0.45

type PaneRect = { x: number; y: number; width: number; height: number; rx: number }

/** Rest + hover-target geometry as CSS custom properties consumed by the
 *  `.pi-pane` rules in index.css. */
function paneVars(rest: PaneRect, hover: PaneRect, open: boolean): CSSProperties {
  return {
    '--pi-x': `${rest.x}px`,
    '--pi-y': `${rest.y}px`,
    '--pi-w': `${rest.width}px`,
    '--pi-h': `${rest.height}px`,
    '--pi-rx': `${rest.rx}px`,
    '--pi-o': open ? LIGHT_OPACITY : 1,
    '--pi-hx': `${hover.x}px`,
    '--pi-hy': `${hover.y}px`,
    '--pi-hw': `${hover.width}px`,
    '--pi-hh': `${hover.height}px`,
    '--pi-hrx': `${hover.rx}px`,
    '--pi-ho': open ? 1 : LIGHT_OPACITY,
  } as CSSProperties
}

function makePanelIcon(side: keyof typeof PANE_GEOMETRY, open: boolean, displayName: string) {
  const rest = PANE_GEOMETRY[side][open ? 'open' : 'closed']
  const hover = PANE_GEOMETRY[side][open ? 'closed' : 'open']
  const vars = paneVars(rest, hover, open)
  function PanelIcon({ size = 24, style, ...props }: PanelIconProps) {
    return (
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        style={style ? { ...vars, ...style } : vars}
        {...props}
      >
        <rect {...FRAME} />
        <rect
          className="pi-pane"
          {...rest}
          fill="currentColor"
          fillOpacity={open ? LIGHT_OPACITY : undefined}
          stroke="none"
        />
      </svg>
    )
  }
  PanelIcon.displayName = displayName
  return PanelIcon
}

/** Left sidebar is HIDDEN — thin solid pane says "click to summon it". */
export const PanelLeftSolid = makePanelIcon('left', false, 'PanelLeftSolid')
/** Left sidebar is OPEN — thick dimmed pane says "already out; click to tuck away". */
export const PanelLeftLight = makePanelIcon('left', true, 'PanelLeftLight')
/** Right panel is CLOSED / "open in panel" affordances — thin solid pane. */
export const PanelRightSolid = makePanelIcon('right', false, 'PanelRightSolid')
/** Right panel is OPEN — thick dimmed pane; clicking closes it. */
export const PanelRightLight = makePanelIcon('right', true, 'PanelRightLight')
/** Bottom dock is CLOSED / "send to bottom dock" affordances — thin solid pane. */
export const PanelBottomSolid = makePanelIcon('bottom', false, 'PanelBottomSolid')
/** Bottom dock is OPEN — thick dimmed pane; also the split-down direction glyph. */
export const PanelBottomLight = makePanelIcon('bottom', true, 'PanelBottomLight')
