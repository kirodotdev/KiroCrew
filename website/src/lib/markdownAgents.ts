/**
 * Markdown-only agents on a harness that cannot load them.
 *
 * An agent defined only as `~/.kiro/agents/<name>.md` runs on the harnesses the
 * gateway names in `markdown_spec_backends` (`ACP_BACKENDS_MARKDOWN_AGENT_SPECS`)
 * and nowhere else: kiro-cli loads JSON specs only, so it refuses the session
 * after the pick. The catalog (`GET /api/agents/catalog`) sends the per-row fact
 * (`markdown_only`), the harness the asking chat runs (`session_backend`) and
 * the capable set; this module turns those into the row's badge. Display-only:
 * the row stays selectable and the gateway's own refusal still explains a pick.
 */

/** Why a row would not start here: the harness it would run on and the ones that can. */
export interface MarkdownBlock {
  /** The harness this chat (or, with no chat, a new session) runs, config spelling. */
  backend: string
  /** The harnesses that load a markdown spec, config spelling, in server order. */
  capable: string[]
}

/** The catalog body's harness facts. Absent on an older gateway. */
export interface MarkdownHarnessFacts {
  session_backend?: unknown
  /** The harnesses that refuse a markdown-only agent: the badge's condition. */
  markdown_refused_backends?: unknown
  /** The harnesses that load one: the badge's remedy. */
  markdown_spec_backends?: unknown
}

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((b): b is string => typeof b === 'string' && b !== '') : []

/**
 * The badge facts for one row, or `undefined` when there is nothing to say.
 *
 * Badges only when the chat's harness is in the gateway's refused set: a host
 * outside it (one that never verifies agent activation) starts the session
 * anyway, so "won't start" would be false there. Quiet on anything unknown: an
 * older gateway that sends no facts, a row with no `markdown_only`, an empty
 * capable set. A badge on a guess would tell a user a runnable agent cannot run.
 */
export function markdownBlock(
  row: { markdown_only?: unknown },
  facts: MarkdownHarnessFacts | undefined,
): MarkdownBlock | undefined {
  if (row.markdown_only !== true || !facts) return undefined
  const backend = facts.session_backend
  if (typeof backend !== 'string' || !backend) return undefined
  if (!strings(facts.markdown_refused_backends).includes(backend)) return undefined
  const capable = strings(facts.markdown_spec_backends)
  if (capable.length === 0 || capable.includes(backend)) return undefined
  return { backend, capable }
}

/** Every row of a catalog body with its `markdown_block` stamped (or cleared). */
export function withMarkdownBlocks<T extends { markdown_only?: unknown }>(
  rows: readonly T[],
  facts: MarkdownHarnessFacts | undefined,
): (T & { markdown_block?: MarkdownBlock })[] {
  return rows.map(row => {
    const block = markdownBlock(row, facts)
    return block ? { ...row, markdown_block: block } : row
  })
}
