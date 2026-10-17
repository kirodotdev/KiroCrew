/**
 * Dismissed "Folders not readable" notices, keyed by project directory.
 *
 * A folder the gateway may not read (a root-owned cache, a mounted volume)
 * can stay that way by design, so `unreadableDirectories` stays populated on
 * every poll and the notice above the tree would be red alarm chrome on
 * every Files visit — wallpaper that dulls the notice when a NEW failure
 * appears. Dismissing the notice remembers the folders it named at that
 * moment; while every folder the payload currently names is remembered, the
 * notice is not rendered (the folder rows keep their lock marker, whose label
 * then says the notice was dismissed). A folder outside the remembered set
 * brings the notice back, with the whole list, and a remembered folder that
 * drops out of the payload is forgotten, so the same folder failing anew
 * alerts again rather than inheriting its old dismissal.
 *
 * Storage is the expansion memory's, under its own key: a module-scope map
 * for the page session (the Files tab remounts on in-place tab navigation),
 * mirrored to `localStorage` so a reload keeps it, bounded and best-effort
 * (`createProjectPathMemory`).
 */

import { createProjectPathMemory } from './treeExpansionMemory'

const dismissals = createProjectPathMemory('mc-files-tree-unreadable-dismissed')

/** Remember the folders whose not-readable notice the user dismissed for a
 *  project directory. Replaces the previous set. */
export function rememberDismissedUnreadable(projectDir: string, folders: readonly string[]): void {
  dismissals.remember(projectDir, folders)
}

/** The dismissed folders for a project directory: session map first, then
 *  `localStorage`, then none. */
export function recallDismissedUnreadable(projectDir: string): readonly string[] {
  return dismissals.recall(projectDir)
}

/** Test-only: drop the module-scope session map so suites stay isolated. */
export function __resetTreeUnreadableDismissalsForTests(): void {
  dismissals.__resetForTests()
}
