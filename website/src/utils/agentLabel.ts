import { i18nT } from '../i18n/t'

/**
 * Display label for an agent field that may be empty (issue #6495).
 *
 * An empty agent means the record resolves the CURRENT default at run time,
 * so the accurate label is the resolved alias — marked `· default` (reusing
 * AgentSelector's badge key) so an inherited default stays distinguishable
 * from an explicit pin. Degrades to the literal 'default' until the default
 * loads or when none is configured. Textual (not a styled badge) because the
 * Worlds scene layers render it as a plain string.
 *
 * The one shared spelling for every surface that renders this state; do not
 * inline copies.
 */
export function agentOrDefaultLabel(agent: string | undefined, defaultAgent: string): string {
  if (agent) return agent
  return defaultAgent ? `${defaultAgent} · ${i18nT('components.agentSelector.default')}` : 'default'
}
