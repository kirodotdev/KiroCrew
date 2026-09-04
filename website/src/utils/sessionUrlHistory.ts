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

/**
 * Whether a Back/Forward (POP) carrying a `?sid=` may switch the active session.
 *
 * The mirror of `shouldReplaceSessionUrl`, and it exists because the two were
 * allowed to disagree. Reading a POP's `?sid=` as "the user retraced to this
 * session" is only sound where a session switch PUSHED the entry being landed
 * on — and on mobile nothing ever pushes one. So on mobile a POP whose `?sid=`
 * differs from the session on screen is never an intent; it is a stale entry
 * pushed by something else at the same URL. Exactly one such entry exists: the
 * duplicate the sessions drawer mints so Back can dismiss it, whose predecessor
 * still names the session the user was on before they opened the drawer.
 *
 * Honouring it is #8207 — tap "+ New", and the pop that closes the drawer walks
 * the pane straight back into the conversation just left, stranding the new
 * empty session in the list. That was patched at the drawer's own pop with a
 * one-shot claim armed before `navigate(-1)`, which only holds while nothing
 * else re-runs the reader in between; a phone browser does not deliver the pop
 * inside the caller's task, so the window is real there and nowhere else. This
 * predicate removes the window instead of racing it: on mobile the reader has
 * nothing to honour, whatever order things land in.
 *
 * Embeds are not covered — a host page drives `?sid=` itself, so there the
 * param IS the instruction and the reader must follow it.
 *
 * `entryPushedBySwitch` is why the layout alone does not decide. The layout is
 * read NOW; the entry was written EARLIER, possibly on the other side of the
 * breakpoint — switch sessions on a wide window, then narrow it (an iPad Mini
 * rotating does exactly this), and Back lands on an entry a real push created.
 * Suppressing there would not merely make Back inert: the reader repairs the sid
 * it declines, so it would overwrite a legitimate history target and destroy it.
 * So provenance comes from the write side, which knows what it pushed.
 */
export function popMaySwitchSession({
  /** Narrow/mobile layout — where a session switch replaces instead of pushing. */
  isMobile,
  /** This entry was pushed BY a session switch, recorded when the push happened. */
  entryPushedBySwitch,
}: { isMobile: boolean; entryPushedBySwitch: boolean }): boolean {
  return !isMobile || entryPushedBySwitch
}
