/** The panel record the two expanded-view capture entries feed the panel read.
 *
 * Shared rather than copied because both captures must show the SAME crew: the
 * pre-mint shot and the mint-failure shot are read side by side, and a reader
 * comparing two different crews cannot tell a state change from a data change.
 *
 * The field names follow `agent_panel_templates/default.html`'s contract -- a
 * `title`, a `subtitle`, prose that becomes the docked lead, scalar stats, and
 * one `<field>_note` caveat -- so the docked summary under the expanded overlay
 * is the real summarizer's output rather than a hand-drawn imitation.
 */
import type { CrewPanelMeta } from '../src/api/client'

/** What the crew published, as the panel read returns it. */
export const PANEL_HTML = '<!doctype html><title>Research crew dashboard</title>'

export const PANEL_META: CrewPanelMeta = {
  template: 'default',
  title: 'Research crew',
  crew: 'research',
  // Fixed rather than computed from `Date.now()`: the docked bar prints a
  // relative age, and a moving clock would make every re-capture a pixel diff.
  published_at: '2026-09-18T10:00:00Z',
  data: {
    title: 'Research crew',
    subtitle: 'Cycle 41',
    finding:
      'Six sources read this cycle. The open question is whether the throughput number holds once the cache is cold, which the next cycle measures.',
    sources_read: 146,
    open_questions: 3,
    throughput: '18/h',
    throughput_note: 'Warm cache only.',
  },
}
