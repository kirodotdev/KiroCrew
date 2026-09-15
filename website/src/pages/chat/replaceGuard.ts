/**
 * Whether a file open may still re-target an existing tab in place.
 *
 * The browser rail navigates by handing `replaceId` — "reuse this tab rather than
 * stacking another" — which is safe only while that tab's buffer is untouched.
 * The caller reads the file asynchronously in between, and that read is exactly
 * when the user can start typing, so the decision has to be re-made at the moment
 * of replacement rather than when the click arrived.
 *
 * Dropping `replaceId` degrades to opening a new tab: the edits stay where they
 * are and the click still takes the user to the file they asked for.
 *
 * Applies to a read that THREW as much as one that succeeded — a dropped
 * connection is not a licence to discard the buffer, and the replacement content
 * in that case is only an error string.
 */
export interface ReplaceableOpts {
  replaceId?: string
  /** Re-asks the source panel whether its buffer is still untouched. */
  canReplace?: () => boolean
}

export function optsForReplace<T extends ReplaceableOpts>(opts?: T): T | undefined {
  if (!opts?.replaceId || !opts.canReplace) return opts
  return opts.canReplace() ? opts : { ...opts, replaceId: undefined }
}
