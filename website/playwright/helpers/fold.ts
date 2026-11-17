import type { CDPSession, Page } from '@playwright/test'

/** The one fold query (src/hooks/useViewportSegments.ts FOLD_X_QUERY). */
const FOLD_X_QUERY = '(horizontal-viewport-segments: 2)'

export interface FoldOptions {
  /** `vertical` puts the fold between a left and a right segment (book pose). */
  orientation: 'vertical' | 'horizontal'
  /** CSS px from the viewport's leading edge to the fold. */
  offset: number
  /** The fold's own width in CSS px; 0 when the segments meet. */
  maskLength: number
  /** The Device Posture the page reads. */
  posture?: 'folded' | 'continuous'
}

/**
 * Emulate a fold in Chromium over CDP, then confirm the page sees it.
 *
 * Tries `Emulation.setDisplayFeaturesOverride` and
 * `Emulation.setDevicePostureOverride` first, then the older `displayFeature`
 * and `devicePosture` fields of `Emulation.setDeviceMetricsOverride`, which
 * some Chromium builds honour when the newer commands are inert. Returns
 * `null` once the fold media query matches, or the reason it never did, so a
 * spec can skip with that reason on an engine that cannot emulate a fold.
 */
/** The CDP session each page's fold was set on: an override lives on the
 *  session that set it, so `withoutFold` clears it there. */
const foldSessions = new WeakMap<Page, CDPSession>()

export async function withFold(page: Page, fold: FoldOptions): Promise<string | null> {
  const viewport = page.viewportSize()
  if (!viewport) return 'the page has no fixed viewport to fold'
  const cdp = await page.context().newCDPSession(page)
  foldSessions.set(page, cdp)
  const feature = { orientation: fold.orientation, offset: fold.offset, maskLength: fold.maskLength }
  const posture = { type: fold.posture ?? 'folded' }
  // A fold report can land a few frames after the override, so poll the query
  // (up to 2 s at 100 ms) before calling a route inert.
  const matches = () => page.waitForFunction(q => window.matchMedia(q).matches, FOLD_X_QUERY, { timeout: 2000, polling: 100 })
    .then(() => true, () => false)
  const tried: string[] = []
  try {
    await cdp.send('Emulation.setDisplayFeaturesOverride' as never, { features: [feature] } as never)
    await cdp.send('Emulation.setDevicePostureOverride' as never, { posture } as never)
    if (await matches()) return null
    tried.push('setDisplayFeaturesOverride had no effect')
  } catch (e) {
    tried.push(`setDisplayFeaturesOverride: ${(e as Error).message}`)
  }
  try {
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: viewport.width, height: viewport.height, deviceScaleFactor: 1, mobile: false,
      displayFeature: feature, devicePosture: posture,
    } as never)
    if (await matches()) return null
    tried.push('setDeviceMetricsOverride displayFeature had no effect')
  } catch (e) {
    tried.push(`setDeviceMetricsOverride: ${(e as Error).message}`)
  }
  return `this browser does not report ${FOLD_X_QUERY} under CDP fold emulation (${tried.join('; ')})`
}

/** Flatten a page `withFold` folded: clear the fold on the session that set it.
 *  Returns `null` once the fold media query stops matching, else the reason. */
export async function withoutFold(page: Page): Promise<string | null> {
  const cdp = foldSessions.get(page)
  if (!cdp) return 'withFold never folded this page'
  await cdp.send('Emulation.clearDisplayFeaturesOverride' as never).catch(() => {})
  await cdp.send('Emulation.clearDevicePostureOverride' as never).catch(() => {})
  // Poll for the unfold as withFold polls for the fold (up to 2 s at 100 ms).
  const cleared = () => page.waitForFunction(q => !window.matchMedia(q).matches, FOLD_X_QUERY, { timeout: 2000, polling: 100 })
    .then(() => true, () => false)
  if (await cleared()) return null
  await cdp.send('Emulation.clearDeviceMetricsOverride').catch(() => {})
  return (await cleared()) ? null : `${FOLD_X_QUERY} still matches after clearing the fold`
}
