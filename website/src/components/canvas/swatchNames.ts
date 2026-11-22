/**
 * The palette's colour names as people read them, for labels and tooltips. One
 * literal key per swatch so every key is visible to the catalog checks.
 */
import { i18nT } from '../../i18n/t'
import type { Swatch } from './canvasModel'

const NAMES: Record<Swatch, () => string> = {
  black: () => i18nT('components.canvas.swatch_black'),
  dark: () => i18nT('components.canvas.swatch_dark'),
  red: () => i18nT('components.canvas.swatch_red'),
  orange: () => i18nT('components.canvas.swatch_orange'),
  yellow: () => i18nT('components.canvas.swatch_yellow'),
  green: () => i18nT('components.canvas.swatch_green'),
  teal: () => i18nT('components.canvas.swatch_teal'),
  blue: () => i18nT('components.canvas.swatch_blue'),
  violet: () => i18nT('components.canvas.swatch_violet'),
  magenta: () => i18nT('components.canvas.swatch_magenta'),
  white: () => i18nT('components.canvas.swatch_white'),
  silver: () => i18nT('components.canvas.swatch_silver'),
  smoke: () => i18nT('components.canvas.swatch_smoke'),
  rose: () => i18nT('components.canvas.swatch_rose'),
  peach: () => i18nT('components.canvas.swatch_peach'),
  cream: () => i18nT('components.canvas.swatch_cream'),
  mint: () => i18nT('components.canvas.swatch_mint'),
  ice: () => i18nT('components.canvas.swatch_ice'),
  sky: () => i18nT('components.canvas.swatch_sky'),
  lavender: () => i18nT('components.canvas.swatch_lavender'),
  blush: () => i18nT('components.canvas.swatch_blush'),
}

export const swatchName = (c: Swatch): string => NAMES[c]()
