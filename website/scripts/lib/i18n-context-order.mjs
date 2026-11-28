/**
 * Key order for the `entries` of `src/i18n/en.context.json`, and the replay that
 * moves a branch's own entry changes onto a file already in that order.
 *
 * One definition of the order, shared by `scripts/i18n-context-sort.mjs`, which
 * writes it, and `contextSidecar.test.ts`, which fails on an entry out of it: the
 * default string sort, UTF-16 code units, which for these ASCII keys is byte order.
 */

/** The same entries, in key order. */
export function inKeyOrder(entries) {
  return Object.fromEntries(Object.keys(entries).sort().map(key => [key, entries[key]]))
}

/**
 * `entries` with one revision's own entry changes applied: every key the revision
 * added or gave a new value, read against its parent, takes the revision's value,
 * and every key it deleted goes. A key the revision did not touch keeps the value
 * in `entries`, so a change main made since the branch forked survives.
 */
export function replayEntries(entries, parent, revision) {
  const replayed = { ...entries }
  for (const key of Object.keys(parent)) {
    if (!Object.hasOwn(revision, key)) delete replayed[key]
  }
  for (const [key, value] of Object.entries(revision)) {
    if (!Object.hasOwn(parent, key) || parent[key] !== value) replayed[key] = value
  }
  return replayed
}
