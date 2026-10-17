import { api } from './client'

/**
 * The one `queryFn` for the shared `['dashboardConfig']` query.
 *
 * Its identity is what matters. TanStack Query's `QueryObserver.setOptions`
 * compares each render's options shallowly and emits an
 * `observerOptionsUpdated` cache event when they differ, and an inline
 * `queryFn: () => api.dashboardConfig()` is a new function on every render.
 * With a dozen mounted readers of this key, a keystroke in the composer fanned
 * out into cache events for every one of them, and every cache-wide subscriber
 * (devtools, `useIsFetching`) re-ran on each. A module-level function keeps the
 * options equal, so a re-render emits nothing.
 *
 * It still calls `api.dashboardConfig` at fetch time rather than capturing it,
 * so tests that mock the client keep working.
 */
export const fetchDashboardConfig = () => api.dashboardConfig()
