import { i18nT } from '../i18n/t'
import type { MemorySource } from '../lib/agentMemorySource'

/**
 * An agent label marked with the memory it runs on, for a session whose agent
 * shares its name with the other kind (#19472): `reviewer · own memory` on the
 * crewmate, `reviewer · shared memory` on the custom agent. `source` comes from
 * `sessionMemoryTag`, which is null for every name without a same-name pair, so
 * the label is unchanged then.
 */
export function withMemorySource(label: string, source: MemorySource | null): string {
  if (!source) return label
  return source === 'own'
    ? i18nT('components.agentDropdownList.label_own_memory', { label })
    : i18nT('components.agentDropdownList.label_shared_memory', { label })
}

/**
 * Display label for an agent field that may be empty (issue #6495).
 *
 * An empty agent means the record resolves the CURRENT default at run time,
 * so the accurate label is the resolved alias — marked `· default` (reusing
 * AgentSelector's badge key) so an inherited default stays distinguishable
 * from an explicit pin. The built-in wire alias `default` and the English marker
 * render as the same word, so that one combination shows it once. A named alias
 * remains marked even when its spelling happens to equal a translated marker:
 * the first word is the agent's identity and the second is its inherited state.
 * Degrades to the literal `default` until the default loads or when none is
 * configured. The inherited-default tooltip also explains the dynamic binding
 * on interactive surfaces. Textual (not a styled badge) because the Worlds scene
 * layers render it as a plain string.
 *
 * The one shared spelling for every surface that renders this state; do not
 * inline copies.
 */
export function agentOrDefaultLabel(
  agent: string | undefined,
  defaultAgent: string,
  kind?: string,
): string {
  // `kind` is the namespace the session recorded for `agent` (`ChatSlot.agent_kind`).
  // Only a recorded `template` pick gets a built-in display name: a crewmate that
  // shares the name keeps it, and a kind-less pin is not guessed from its name.
  if (agent) return kind === 'template' ? builtinAgentDisplayName(agent) : agent
  if (!defaultAgent) return 'default'
  const defaultLabel = i18nT('components.agentSelector.default')
  // An agent-less session runs the default TEMPLATE (#18342).
  const shown = builtinAgentDisplayName(defaultAgent)
  return shown === 'default' && defaultLabel === 'default'
    ? defaultLabel
    : `${shown} · ${defaultLabel}`
}

/**
 * Plain names for built-in provider templates whose spec name is a code name
 * (#21531). Display only: the raw id stays the key for identity, routing and
 * search everywhere else. Returns the raw name for every other agent.
 */
export function builtinAgentDisplayName(name: string): string {
  switch (name) {
    case 'kirocrew-conductor': return i18nT('components.agentLabel.builtin_conductor')
    default: return name
  }
}
