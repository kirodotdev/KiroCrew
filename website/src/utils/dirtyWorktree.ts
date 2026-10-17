/**
 * The gateway refuses `POST /api/update` with `409 {code: "dirty_worktree",
 * changed: <n>}` when the checkout has uncommitted changes. The refusal is the
 * safe outcome (the update would otherwise overwrite local edits), so the UI's
 * job is only to say what to do next.
 */
import { parseErrorCode } from './errorReport'

/**
 * The changed-entry count of a `dirty_worktree` refusal, or `null` for any
 * other error. A refusal without a usable count reports 0 so the guided copy
 * still renders. Duck-typed on `status` and `body`, like `isNotFoundError`, so
 * it reads an `ApiError` from any transport.
 */
export function dirtyWorktreeChanged(e: unknown): number | null {
  if (typeof e !== 'object' || e === null) return null
  const { status, body } = e as { status?: unknown; body?: unknown }
  if (status !== 409 || typeof body !== 'string') return null
  if (parseErrorCode(body) !== 'dirty_worktree') return null
  try {
    const changed = (JSON.parse(body) as { changed?: unknown }).changed
    return typeof changed === 'number' && Number.isFinite(changed) && changed >= 0 ? changed : 0
  } catch {
    return 0
  }
}
