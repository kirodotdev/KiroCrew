/**
 * The sandbox grants for a crewmate's dynamic dashboard, and the ONE line of
 * this file that is a security boundary rather than a layout choice.
 *
 * `allow-scripts` alone, which contract v3 requires (`html` MAY run JS: a chart
 * is script or it is nothing) and nothing more. Every other grant is withheld:
 *
 * - No `allow-same-origin`, so the document lands on a null (opaque) origin and
 *   cannot read the dashboard's cookies or localStorage, or touch the parent DOM.
 * - No `allow-popups`. A template is loaded at run time, including one a user
 *   imported from a shared file, and a window it could open is a capability
 *   nothing about a status page needs. The one way out is the host's own
 *   `kirocrew-dashboard:open` bridge (`useFrameOpenLink`): a GitHub pull-request
 *   URL, from this frame, right after a user gesture, opened by the host.
 * - No `allow-top-navigation`, `allow-forms`, or `allow-modals`.
 *
 * Egress is closed by the document CSP (`connect-src 'none'`), which the gateway
 * injects inside the composed document AND `buildSrcdoc` injects outside it.
 * Policies combine by intersection, so the stricter wins on every directive.
 *
 * Same value as the published-panel frame deliberately: these are two documents
 * on one surface, and a reader comparing them should not have to wonder whether
 * one of them is more trusted.
 */
export const CREW_DASHBOARD_SANDBOX = 'allow-scripts'
