/**
 * `-webkit-app-region`, typed once.
 *
 * Electron uses this property to decide which parts of a frameless window drag it.
 * React's `CSSProperties` does not declare it, so the desktop app cast `as any` at
 * every site — and one of those casts sat in a DUPLICATE `style` attribute, where
 * JSX silently kept only the last one and dropped the opt-out entirely. Declaring it
 * properly, in one place, removes both the casts and that failure mode.
 */
import type { CSSProperties } from 'react'

/** `CSSProperties` plus the Electron-only drag property. */
export type AppRegionStyle = CSSProperties & {
  WebkitAppRegion?: 'drag' | 'no-drag'
}

/**
 * Marks a surface as the window's drag handle.
 *
 * Spread it rather than assigning, so it composes with the element's own styles
 * instead of replacing them — which is precisely how the dropped-attribute bug
 * happened.
 */
export const DRAG_REGION: AppRegionStyle = { WebkitAppRegion: 'drag' }

/**
 * Opts an interactive element OUT of an enclosing drag region.
 *
 * Required on every control inside a draggable header: without it the drag handler
 * swallows the click and the button silently stops working.
 */
export const NO_DRAG_REGION: AppRegionStyle = { WebkitAppRegion: 'no-drag' }
