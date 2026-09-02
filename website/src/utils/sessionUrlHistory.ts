/**
 * Whether a `?sid=` URL write should REPLACE the current history entry rather
 * than push a new one.
 *
 * Extracted from ChatPage's URL-sync effect so the invariant is greppable and
 * pinned by a test: it is a one-line predicate buried in a 60-line effect, and
 * the mobile clause in particular reads as a harmless simplification to anyone
 * who does not know why it is there.
 *
 * DESKTOP pushes on a real session switch, so Back/Forward retraces the
 * sessions you visited — there is a keyboard Back and a visible history, and
 * retracing is what a user expects of them.
 *
 * MOBILE always replaces. Back there is a left-edge swipe the platform owns and
 * the only back affordance the layout has, and the surfaces a user actually
 * opens on mobile — the sessions drawer, the side panel, the file viewer — are
 * component state that pushes nothing. So a pushing session switch made Back
 * skip every layer the user had opened and land in an unrelated earlier chat
 * instead. Replacing leaves mobile Back meaning exactly one thing: leave the
 * chat route.
 */
export function shouldReplaceSessionUrl({
  /** A prior `?sid=` exists and names a DIFFERENT session (a real switch). */
  isSessionSwitch,
  /** Narrow/mobile layout — the one with no back affordance of its own. */
  isMobile,
}: { isSessionSwitch: boolean; isMobile: boolean }): boolean {
  return !isSessionSwitch || isMobile
}
