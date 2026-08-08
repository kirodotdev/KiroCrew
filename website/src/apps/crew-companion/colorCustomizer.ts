/**
 * CrewCompanion - SVG Color Customizer
 *
 * Pure functions for SVG color extraction, replacement, and ColorMap management.
 * No Electron or DOM dependencies — safe for both main and renderer processes.
 */

/** Color mapping: source hex → target hex (both #RRGGBB format) */
export type ColorMap = Record<string, string>

/** Validates exactly 3 or 6 hex digits after # */
const HEX_COLOR_RE = /^#(?:[0-9A-Fa-f]{3}){1,2}$/

/** Shared regex for extracting fill/stroke hex colors from SVG attributes */
const SVG_COLOR_ATTR_RE = /(fill|stroke)="(#(?:[0-9A-Fa-f]{3}){1,2})"/gi

export function isValidHexColor(color: string): boolean {
  return HEX_COLOR_RE.test(color)
}

export function normalizeHexColor(color: string): string {
  if (!isValidHexColor(color)) return color
  const hex = color.slice(1).toUpperCase()
  if (hex.length === 3) return `#${hex[0]}${hex[0]}${hex[1]}${hex[1]}${hex[2]}${hex[2]}`
  return `#${hex}`
}

export function extractSvgColors(svgContent: string): string[] {
  const seen = new Set<string>()
  const re = new RegExp(SVG_COLOR_ATTR_RE.source, SVG_COLOR_ATTR_RE.flags)
  let m: RegExpExecArray | null
  while ((m = re.exec(svgContent)) !== null) {
    seen.add(normalizeHexColor(m[2]))
  }
  return [...seen]
}

export function applySvgColorMap(svgContent: string, colorMap: ColorMap): string {
  if (!svgContent || Object.keys(colorMap).length === 0) return svgContent
  const safe = sanitizeColorMap(colorMap)
  if (Object.keys(safe).length === 0) return svgContent
  // Build normalized lookup: uppercase key → target value
  const lookup = new Map<string, string>()
  for (const [src, dst] of Object.entries(safe)) {
    lookup.set(normalizeHexColor(src), dst)
  }
  return svgContent.replace(
    new RegExp(SVG_COLOR_ATTR_RE.source, SVG_COLOR_ATTR_RE.flags),
    (full, attr, color) => {
      const norm = normalizeHexColor(color)
      const target = lookup.get(norm)
      return target ? `${attr}="${target}"` : full
    },
  )
}

export function serializeColorMap(colorMap: ColorMap): string {
  return JSON.stringify(colorMap)
}

export function parseColorMap(json: string): ColorMap {
  try {
    const parsed = JSON.parse(json)
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return {}
    return sanitizeColorMap(parsed)
  } catch {
    return {}
  }
}

export function sanitizeColorMap(colorMap: ColorMap): ColorMap {
  const result: ColorMap = {}
  for (const [k, v] of Object.entries(colorMap)) {
    if (isValidHexColor(k) && isValidHexColor(v)) result[k] = v
  }
  return result
}
