/**
 * True when this dashboard SPA is running as an embedded remote-instance pane
 * (the full dashboard loaded inside an <iframe> by InstancesViewport.srcFor).
 *
 * The instances feature is single-level by design: an embedded pane must NOT
 * show its own instances switcher. Otherwise a remote could connect onward to
 * yet another remote, nesting dashboards and stacking SSH tunnels into a deep,
 * confusing chain. Suppressing the switcher when embedded enforces this
 * automatically, with no per-host config.
 *
 * Detection: the full dashboard chrome only renders OUTSIDE the /embed/* routes
 * (those render a focused ChatPage, not the instances UI), so "full dashboard
 * inside an iframe" reliably means "instance pane". A cross-origin parent makes
 * window.top access throw — that also means we are embedded, so treat the throw
 * as embedded=true.
 */
export function isEmbeddedPane(): boolean {
  try {
    return typeof window !== 'undefined' && window.self !== window.top
  } catch {
    return true
  }
}
