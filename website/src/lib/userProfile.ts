/**
 * Shared rules for the free-text "Other" role (`dashboard.user_role_other`).
 *
 * Lives here rather than in each surface because onboarding step 2 and
 * Settings → Chat → About You both write the SAME config key, and the backend
 * rejects anything the two disagree about: the cap is enforced by the PATCH
 * allowlist (`_EDITABLE_CONFIG` in `dashboard/handlers/core.py`) and re-applied
 * when the value is quoted into the prompt (`_ROLE_OTHER_MAX_LEN` in
 * `context.py`). Two hand-maintained copies of the number is exactly the drift
 * that turns into a silent 400.
 */

/** Max length of the custom role, in Unicode code points. */
export const ROLE_OTHER_MAX_LEN = 60

/**
 * Cap a value being TYPED, counting code points and preserving whitespace.
 *
 * Counts by CODE POINT, not UTF-16 code unit, because Python's `len()` — what
 * the PATCH validator uses — counts code points. A `String.prototype.slice`
 * boundary (or an HTML `maxLength`, which is also code-unit based) can land
 * between the halves of a surrogate pair: pasting 59 BMP characters followed by
 * an astral character such as `😀` would otherwise leave a lone high surrogate,
 * which renders as a replacement glyph on reload and is dropped again by the
 * prompt sanitizer.
 *
 * Deliberately does NOT trim: eating a trailing space mid-typing would make a
 * two-word role ("solutions architect") impossible to type.
 */
export function capRoleOther(value: string): string {
  return Array.from(value).slice(0, ROLE_OTHER_MAX_LEN).join('')
}

/** Normalize a value being PERSISTED: trim the edges, then cap. */
export function clampRoleOther(value: string): string {
  return capRoleOther(value.trim())
}
