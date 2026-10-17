/**
 * The episode ids of the proxy-challenge capture sheet, shared by the scene that
 * renders them and the runner that asserts them.
 *
 * One list, imported by both, because the two drifted: the runner waited for ten
 * episodes while the scene rendered five, so the documented command timed out and
 * wrote no frame. A test could only notice that after the fact; a shared list makes
 * it unrepresentable.
 *
 * Plain `.mjs` deliberately — the runner is a node script and cannot import `.tsx`.
 */
export const EPISODES = ['before', 'challenged', 'framed', 'rejected']
