/**
 * The update-card capture harness must render NON-BLANK.
 *
 * AboutPanel renders a react-router <Link> (the "view all releases" link), so
 * any mount of it needs a router in the provider stack. The capture entry
 * (capture/update-card.tsx) is such a mount: without its router the Link
 * throws during render, the tree unmounts, and every scene screenshots blank --
 * scripts/capture-update-card.mjs then has no truthful evidence frames. The
 * capture script does not run in CI, so this test is the guard.
 *
 * Two halves:
 *   1. the MECHANISM pin: AboutPanel genuinely cannot mount outside a router,
 *      which is the fact that obliges the harness to carry one. If this ever
 *      starts passing (AboutPanel dropped its router dependency), the source
 *      contract below is what should be revisited.
 *   2. the SOURCE contract on the harness file itself: a router wrapper
 *      encloses the <AboutPanel /> mount. Matched by property (any react-router
 *      *Router element), not exact spelling, so adding props to the router or
 *      switching router flavors does not break it. Layout is not asserted --
 *      the same split TranscriptRowGeometry.test.tsx uses for capture-backed
 *      evidence.
 */
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { AboutPanel } from '../pages/settings/AboutPanel'

const HARNESS_PATH = join(__dirname, '..', '..', 'capture', 'update-card.tsx')

describe('update-card capture harness', () => {
  it('AboutPanel cannot mount without a router (the mechanism the harness must satisfy)', () => {
    // React logs the uncaught render error before rethrowing; silence it so the
    // EXPECTED throw does not read as noise in the run output.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      expect(() =>
        render(
          <Provider store={createTestStore()}>
            <QueryClientProvider client={qc}>
              <AboutPanel />
            </QueryClientProvider>
          </Provider>,
        ),
      ).toThrow()
    } finally {
      spy.mockRestore()
    }
  })

  it('keeps a router wrapper around the harness mount (source contract)', () => {
    const src = readFileSync(HARNESS_PATH, 'utf8')
    // Any react-router router element counts; [\s>] admits props on the tag.
    const open = src.search(/<(?:Memory|Browser|Hash)Router[\s>]/)
    const close = src.search(/<\/(?:Memory|Browser|Hash)Router>/)
    const panel = src.indexOf('<AboutPanel />')
    expect(open).toBeGreaterThan(-1)
    expect(panel).toBeGreaterThan(open)
    expect(close).toBeGreaterThan(panel)
  })
})
