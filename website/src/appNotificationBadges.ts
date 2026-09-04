/**
 * Per-app rail badge counts, derived from notifications the app already published.
 *
 * An app that wants to signal "something is waiting for you" already has a
 * public route for it, and it is not a new one: it declares
 * `notifications.channels[]` in its manifest (`apps/manifest.py`
 * `NotificationChannel`) and pushes through `POST /api/notifications/push`.
 * The HOST stamps the producer onto every stored record as `source:
 * "app:<name>"` -- `notifications_push.py` marks that "server-resolved; body
 * cannot override" -- and `acked` is a RESERVED note key, so an app can neither
 * forge another app's attribution nor pre-acknowledge its own note.
 *
 * So the number a rail badge needs is already in the notification list the
 * dashboard fetches. Nothing here asks an app to declare a new manifest field
 * or implement a new endpoint; this module is only the join the dashboard was
 * missing: unacknowledged notes, grouped by the app the host attributed them to.
 *
 * Why the badge cannot simply come from the app's own frontend: `useNavBadge()`
 * exists and is public, but it is a hook inside the app's UI bundle, which is
 * imported when its page mounts. Before the user has opened the app there is no
 * app code running to call it -- which is exactly the case a "something is
 * waiting" badge is for. A notification pushed by the app's BACKEND does not
 * have that problem.
 */

/**
 * The prefix the host stamps onto an app-published notification's `source`.
 *
 * Server-side spelling is `f"app:{app_name}"`, so the app name is everything
 * after this prefix. Matched as a prefix rather than by splitting on ":"
 * because an app name cannot contain one but a future source scheme might.
 */
export const APP_SOURCE_PREFIX = 'app:'

/**
 * Upper bound on a rendered per-app count.
 *
 * The count is influenced by app-supplied content -- an app decides how many
 * notifications to push -- and it is rendered in the host's own chrome, where
 * an unbounded number is a layout- and spoofing-surface rather than
 * information. Bounding it here keeps the cap on the value this module
 * introduces; it deliberately does not reach into `BadgeIndicator`, whose other
 * callers are host-owned counts and are not this change's business.
 */
export const MAX_APP_BADGE_COUNT = 99

/**
 * Nav-id prefix an AppHost-routed app's rail row carries (`appNav.ts` builds
 * `app-<name>`).
 *
 * Load-bearing for safety, not cosmetics. `NavBadge` keys its fallback lookup on
 * the BARE name for a row without this prefix, so a host row's own id -- e.g.
 * `schedule` -- indexes the same map an app name would. Derived counts must
 * therefore reach ONLY prefixed rows, or an app could put attention on host
 * chrome by choosing its name.
 */
export const APP_NAV_ID_PREFIX = 'app-'

/**
 * Whether *navId* is an installed app's row, and so may show a derived count.
 *
 * Every installed app is AppHost-routed and therefore prefixed; a bare id is a
 * host surface (or a native builtin), which keeps its own badge source.
 */
export function isAppNavId(navId: string): boolean {
  return navId.startsWith(APP_NAV_ID_PREFIX)
}

/**
 * The subset of a notification this derivation reads.
 *
 * Pinned locally rather than importing the full `Notification` type, for the
 * reason `appNav.ts` pins its own `/api/apps` subset: a field this derivation
 * depends on cannot then quietly change shape underneath it. Both fields are
 * optional in the wire type, and both are host-written.
 */
export interface BadgeNote {
  /** Host-stamped producer, e.g. `"app:my-ledger"`. Absent on older records. */
  source?: string
  /** Host-owned read flag. A reserved key: an app cannot set it. */
  acked?: boolean
  /** Channel priority. `"passive"` is explicitly not an attention item. */
  priority?: string
  /** Set when the note's channel is muted. Also a reserved key. */
  silenced?: boolean
}

/**
 * The priority the host already excludes from its own unread count.
 *
 * Not a judgement call made here: `notification_coordinator.py` increments
 * `_unread_count` only `if note.get("priority") != "passive"`, and the feed
 * renders a passive note dimmed as non-attention (`notifMeta.tsx`). Counting
 * one as rail attention would contradict the very subsystem this derives from.
 */
const PASSIVE_PRIORITY = 'passive'

/**
 * Whether *note* is an attention item, by the host's OWN definition.
 *
 * That definition is three clauses, not two, and it is written down in the
 * dashboard already: `!n.acked && n.priority !== 'passive' && !n.silenced`
 * (`App.tsx`, commented "mirroring the backend's `_unread_count` semantics";
 * the same predicate again in `hooks/useWebSocket.ts`, and an existing test
 * pins the expression). This mirrors it rather than inventing a variant.
 *
 * `silenced` matters for more than consistency: muting a channel is the ONLY
 * opt-out that works while the app's page is unmounted, which is exactly the
 * regime this badge covers. Ignoring it would leave a badge no user could
 * switch off without the app's own UI running.
 */
function isAttention(note: BadgeNote): boolean {
  return !note.acked && note.priority !== PASSIVE_PRIORITY && !note.silenced
}

/**
 * Count unacknowledged notifications per producing app.
 *
 * Returns a map of app name to count, carrying an entry ONLY for apps that
 * currently have at least one unacknowledged note -- an app with nothing
 * waiting is absent rather than present-and-zero, so a caller merging this map
 * cannot accidentally clear a count another source set.
 */
export function appNotificationBadges(notes: readonly BadgeNote[]): Record<string, number> {
  // Prototype-less ON PURPOSE. An app name is attacker-chosen, and `manifest.py`
  // reserves only a namespace list -- `constructor`, `toString` and `__proto__`
  // are all valid kebab names. On a plain `{}`, `counts['constructor']` reads the
  // inherited `Object` function, which is truthy, so `+ 1` string-concatenates
  // and the row renders a function body as its count.
  const counts: Record<string, number> = Object.create(null)
  for (const note of notes) {
    // One predicate, mirroring the host's three-clause definition, so a future
    // clause is added in one place rather than drifting between two.
    if (!isAttention(note)) continue
    const source = note.source
    if (!source || !source.startsWith(APP_SOURCE_PREFIX)) continue
    // Keyed by the name the HOST stamped, which is also how `appBadges` and
    // `NavBadge`'s `app-<name>` id are keyed, so the join needs no translation.
    const name = source.slice(APP_SOURCE_PREFIX.length)
    // A bare "app:" names no app: it must not become an empty-string key that
    // matches no rail row and silently accumulates.
    if (!name) continue
    const next = (counts[name] || 0) + 1
    counts[name] = next > MAX_APP_BADGE_COUNT ? MAX_APP_BADGE_COUNT : next
  }
  return counts
}

/**
 * Merge an app's explicitly pushed badge over the derived counts.
 *
 * `pushed` wins on every key it carries, INCLUDING an explicit `0`: an app that
 * called `useNavBadge(0)` has stated it wants no badge, and a count derived
 * from its notifications must not overrule that statement.
 *
 * The consequence is the property that makes this safe to ship: an app already
 * using the SDK hook today keeps exactly its current behaviour, because its key
 * is present in `pushed` and therefore always wins. The derivation only fills
 * rows that had no badge at all.
 */
export function mergeAppBadges(
  pushed: Record<string, number>,
  derived: Record<string, number>,
): Record<string, number> {
  // Prototype-less for the SECOND half of the same hazard, which is one level
  // out from where the count is built: `{ ...derived, ...pushed }` produces a
  // normal object literal, so a LOOKUP of `railAppBadges['constructor']` for an
  // app with no entry would resolve to the inherited `Object` function -- truthy,
  // so `NavBadge`'s `|| 0` does not catch it and `BadgeIndicator`'s `count <= 0`
  // does not either. Fixing only the accumulator leaves the read reachable.
  // `Object.assign` copies own enumerable keys only, so precedence is unchanged.
  return Object.assign(Object.create(null) as Record<string, number>, derived, pushed)
}
