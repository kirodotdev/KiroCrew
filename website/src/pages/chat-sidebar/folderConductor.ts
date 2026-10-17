/**
 * Which session CONDUCTS a sidebar folder — derived, never stored.
 *
 * A folder often holds one session that opened every other session in it: a
 * conductor and its workers. Reaching that conversation means expanding the
 * folder and picking its card out of the lane, so the folder row — the one row
 * on screen while the folder is collapsed — offers no way to it.
 *
 * This is a PURE function of data the sidebar already renders from: `folder_id`
 * (folder membership, applied by the caller, which hands in one folder's rows)
 * and `parent.key` (who opened whom). No backend field, no new API, no persisted
 * "this folder's conductor" record — which is the point. A stored pointer would
 * be a second source of truth that goes stale the moment a session closes or is
 * moved between folders, and it would have to be migrated for every folder that
 * already exists.
 *
 * The tree itself comes from `lib/sessionLineage.ts`, the same module the
 * conductor lane nests rows by, so "who is on top of this folder" is answered by
 * the structure the sidebar already draws. A second copy of that walk would be
 * free to disagree with it — and would, on the payloads `nestsUnder` treats
 * specially: a row citing a creator that is not running, and a records cycle.
 *
 * Deliberately structural over `Slot`: the function takes the fields it reads, so
 * a test states a case as literals instead of a slot payload, and a change to
 * `Slot` cannot silently change what counts as a conductor.
 */
import { buildLineage, descendantsOf, type LineageRow } from '../../lib/sessionLineage'

/** The least a row must carry to be placed. A subset of the sidebar's `Slot`. */
export interface FolderConductorRow extends LineageRow {
  /** ISO creation instant, used only to break a tie deterministically. */
  created?: string
}

/**
 * The row key of the folder's conductor, from the folder's OWN rows, or null.
 *
 * A row conducts this folder when both hold:
 *
 *   1. it opened at least one other row filed in the same folder, and
 *   2. nothing filed in the same folder opened IT.
 *
 * Rule 1 is what makes the claim "this session manages the folder" true of a
 * particular row rather than of whichever row happens to look like an agent. Rule
 * 2 is what keeps a mid-level worker that dispatched its own helpers from
 * outranking the session above it: the conductor is the TOP of the folder's own
 * lineage, so one folder always resolves to at most one conductor.
 *
 * Both rules read the placed tree, which is how they stay in step with the lane:
 * rule 2 is "a root of this folder's lineage" and rule 1 is "that root has
 * children". A row whose creator is not running, or whose records formed a cycle,
 * is a root HERE for exactly the reason it is drawn as one there.
 *
 * Neither rule consults the agent name. A folder is conducted because of what its
 * sessions DID, and an agent-name guess would be wrong in both directions — a
 * session running a conductor agent that has dispatched nothing yet is not
 * conducting anything the user can see, and a plain session that dispatched six
 * workers is.
 *
 * Ties (two independent roots each with workers) are decided by subtree size,
 * then by the earlier `created`, then by `key`. The order never matters for
 * correctness — it exists so two renders of one payload agree.
 *
 * Returns null for a folder with no such row, and such a folder keeps a row with
 * nothing added to it. That is the fallback the whole feature rests on: the
 * ordinary folder is not a degraded case of a conducted one.
 */
export function detectFolderConductor<R extends FolderConductorRow>(
  rows: readonly R[],
): string | null {
  if (rows.length < 2) return null
  const { roots, children } = buildLineage(rows)
  const byKey = new Map<string, R>()
  for (const row of rows) if (row.key) byKey.set(row.key, row)

  let best: { row: R; workers: number } | null = null
  for (const key of roots) {
    const workers = descendantsOf(key, children).length
    if (workers === 0) continue
    const row = byKey.get(key)
    if (row === undefined) continue
    if (best == null || compareCandidates(row, workers, best.row, best.workers) < 0) {
      best = { row, workers }
    }
  }
  return best?.row.key ?? null
}

/** Negative when *a* should win. Total, so the result cannot depend on input order. */
function compareCandidates<R extends FolderConductorRow>(
  a: R, aWorkers: number, b: R, bWorkers: number,
): number {
  if (aWorkers !== bWorkers) return bWorkers - aWorkers
  const aCreated = a.created ?? ''
  const bCreated = b.created ?? ''
  // A row with no `created` sorts last rather than first: an absent instant is
  // unknown, and treating "" as the earliest would let a payload gap outrank a
  // real timestamp.
  if (aCreated !== bCreated) {
    if (aCreated === '') return 1
    if (bCreated === '') return -1
    return aCreated < bCreated ? -1 : 1
  }
  return a.key < b.key ? -1 : a.key > b.key ? 1 : 0
}
