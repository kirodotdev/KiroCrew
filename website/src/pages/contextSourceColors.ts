/**
 * Per-SOURCE hues for context-composition bars, shared by the per-turn Context
 * Breakdown panel and the Session Breakdown tree so the two ALWAYS agree on a
 * source's colour (history is the same amber in both, skill the same green).
 *
 * Hue is the data channel here: a bar stacks many sources in one short row and
 * is compared across rows/nodes, so a size-ranked grey ramp made the two
 * surfaces disagree and buried the "what is this made of" signal. Each hue is
 * mixed into the surface (see index.css `--ctx-src-*`) so it reads as a calm
 * instrument fill on any theme rather than a saturated slop-gradient; the long
 * tail of unrecognised sources falls to a neutral mute. The user's own text
 * stays on the accent (handled by the callers), matching both surfaces.
 */

/** Label -> CSS var. Absent labels fall to the mute. */
const SOURCE_HUE: Record<string, string> = {
  loaded_skill: 'var(--ctx-src-skill)',
  memory: 'var(--ctx-src-memory)',
  semantic_memory: 'var(--ctx-src-memory)',
  episodic_memory: 'var(--ctx-src-memory)',
  history: 'var(--ctx-src-history)',
  conversation_replay: 'var(--ctx-src-history)',
  channel_context: 'var(--ctx-src-history)',
  history_prefix: 'var(--ctx-src-history)',
  lessons: 'var(--ctx-src-lessons)',
  agent_instructions: 'var(--ctx-src-sys)',
  critical_rules: 'var(--ctx-src-sys)',
  every_turn: 'var(--ctx-src-sys)',
  skill_index: 'var(--ctx-src-skillidx)',
  skill_hint: 'var(--ctx-src-skillidx)',
  hook_context: 'var(--ctx-src-tool)',
  tool_output: 'var(--ctx-src-tool)',
}

export const SOURCE_MUTE = 'var(--ctx-src-mute)'

/** The fill for one context-source label. */
export function sourceFill(label: string): string {
  return SOURCE_HUE[label] ?? SOURCE_MUTE
}

/** A readable foreground for a label drawn ON its own fill. The hues are all
 *  mixed toward the surface, so the theme's own strong text reads on every one;
 *  return it as the single contrasting choice rather than a per-hue table. */
export function sourceFg(): string {
  return 'var(--text-strong, var(--text))'
}
