/**
 * Stylesheet text for the library card's SVG. SvgShadow (components/SvgShadow.tsx)
 * puts it inside the SVG's own shadow root, where the page's classes cannot reach:
 * it fits the outer `<svg>` to the tile and leaves the SVGs nested in it alone.
 */
export const THUMB_SVG_CSS = ':host > svg { max-width: 100%; max-height: 280px; height: auto; }'
