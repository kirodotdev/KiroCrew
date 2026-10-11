/** Display labels learned from the backend's model list, keyed by model id.
 *
 *  Filled when a live `/api/models` list is read (providers/adapters/acp.ts) and
 *  consulted by display-only surfaces that hold a bare id but no list, such as
 *  the per-turn footer in chat/AssistantMessage.tsx. Module-level on purpose:
 *  those surfaces render far below the picker and cannot reach the list through
 *  props or a query hook. Dependency-free so any module can import it.
 *
 *  Never feed a label into a write or a comparison: the id is the identity,
 *  the label is only what a person reads. */

const labels = new Map<string, string>()

/** Record the label for `id`. A missing or empty label removes any earlier one,
 *  so a row that no longer carries a label cannot leave a stale name behind. */
export function setModelLabel(id: string, label: string | undefined): void {
  if (!id) return
  if (label) labels.set(id, label)
  else labels.delete(id)
}

/** The learned label for `id`, or undefined when none was recorded. */
export function getModelLabel(id: string): string | undefined {
  return labels.get(id)
}

/** Forget every learned label. Called with the cached model list when the
 *  backend changes, since the labels belong to the previous backend's list. */
export function clearModelLabels(): void {
  labels.clear()
}
