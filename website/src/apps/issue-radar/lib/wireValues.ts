/**
 * PROTOCOL VALUES for the pull-request action surface — and nothing else.
 *
 * Every string in this module is compared **by value** against something outside the
 * dashboard: the server's own action allowlist (`routes._BULK_PR_ACTIONS`), a provider
 * merge-state enum (GitHub's `mergeable_state` / GitLab's `detailed_merge_status`), or
 * a literal the user must TYPE to arm an irreversible action. Translating any of them
 * would break the comparison — for the confirmation tokens it would make the action
 * impossible to complete for anyone not typing English, which is exactly the defect
 * `src/i18n/destructiveConfirm.test.ts` exists to prevent.
 *
 * ## Why these live in their own module
 *
 * They are machine values that happen to be spelled with lowercase letters and
 * underscores, so no *shape* rule can tell them from copy: `'merge prs'` has the same
 * shape as English prose, and `'has_hooks'` differs from a word only by an underscore.
 * Widening `eslint.i18n.config.js`'s `words.exclude` to admit them was measured and
 * rejected — it retroactively dropped 35 strings across 5 unrelated files
 * (`api/client.ts` 33→29, `ChatInput.tsx` 23→20, `TrustDropdown.tsx` 2→0, …), which is
 * precisely the "ratchet that silently hands back unrelated files' debt" that config
 * refuses for its own `next` exclusion. Each of those files deserves its own decision,
 * not this change's coattails.
 *
 * So the exemption is scoped to this ONE module, the same way
 * `src/lib/commitProfiler.tsx` (console-only diagnostics) and
 * `src/apps/md-notebook/styles.ts` (pure CSS) are scoped. **Keep this module
 * protocol-only**: any user-visible copy added here belongs in the catalog instead,
 * and putting it here would silently exempt it.
 */

/** The literal a user types to arm a bulk CLOSE. */
export const BULK_PR_CLOSE_TOKEN = 'close prs'

/** The literal a user types to arm a sequential MERGE.
 *
 * Deliberately different from the close token: merging is irreversible where closing is
 * not, so the two must never be satisfiable by the same typing. Neither shares a value
 * with any button label in any of the ten catalogs, so a confirmation can never be met
 * by copying the button just pressed. */
export const SEQUENTIAL_MERGE_TOKEN = 'merge prs'

/** The bulk bar's pseudo-action for the sequential merge.
 *
 * Not a `BulkPrAction`: it never reaches `/pulls/bulk` (the server's allowlist
 * deliberately excludes `merge`), so it stays out of that union — the type is the wire
 * contract, and widening it would suggest the endpoint accepts this verb. */
export const MERGE_READY_ACTION = 'merge_ready'

/** The merge methods, in the provider-neutral vocabulary the API speaks.
 *
 * Wire values (the server compares them by value and forwards them to the provider),
 * so they are spelled here rather than inlined at each call site. The bulk bar's
 * confirmation NAMES the method it will use, and that copy has to be derived from the
 * value actually sent: a hardcoded `'SQUASH'` at the call site and a separately
 * hardcoded "squash" in the sentence are two places to change and one to forget. */
export const MERGE_METHOD = {
  merge: 'MERGE',
  squash: 'SQUASH',
  rebase: 'REBASE',
} as const

/** The method the bulk bar's sequential merge sends.
 *
 * Named once so the value sent and the method named in the confirmation are the SAME
 * symbol. Squash because it is the only method both providers accept unconditionally
 * (GitLab has no rebase option on `/merge`), and this surface offers no picker. */
export const SEQUENTIAL_MERGE_METHOD = MERGE_METHOD.squash

/** Provider merge-state values that mean the PR's protections are SATISFIED.
 *
 * Mirrors the server's `_MERGE_ALLOWED_STATES`. `unstable` is deliberately absent: it
 * does not distinguish a failing REQUIRED check from a failing optional one, so it
 * cannot be read as "protections satisfied" — and a gate that cannot tell must refuse.
 * `blocked`, `behind`, `dirty`, `draft` and `unknown` are absent for the same reason.
 * So is GitLab's LEGACY `can_be_merged`, which comes from the old `merge_status` field
 * and reports only "no conflicts" — it knows nothing about unmet approvals or a red
 * required pipeline, so admitting it would reproduce the hole this set closes. The
 * modern `mergeable` (`detailed_merge_status`) does imply those rules are met.
 * Such a PR is still one click from auto-merge, which lets the provider decide. */
export const MERGE_READY_STATES = new Set(['clean', 'has_hooks', 'mergeable'])

/** The provider merge state meaning "the branches CONFLICT". Arming auto-merge waits
 * for checks, and no check resolves a conflict, so it is not armable either. */
export const MERGE_STATE_DIRTY = 'dirty'

/** The provider merge state meaning "not computed yet". GitHub answers this on a cold
 * read (measured at roughly half a page on a busy repo), so it must be treated as
 * "cannot tell" rather than as a verdict in either direction. */
export const MERGE_STATE_UNKNOWN = 'unknown'
