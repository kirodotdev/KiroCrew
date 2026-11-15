/**
 * A `$name` skill token, as the backend expander (`kiro_crew.skills._DOLLAR_SKILL_PATTERN`)
 * reads it. The one frontend copy: `test_dispatch_fence_parity.py` holds it to the expander
 * and refuses a second declaration.
 */
export const SKILL_TOKEN_RE = /(?<![\w$])\$[a-z0-9][a-z0-9/_-]*/
