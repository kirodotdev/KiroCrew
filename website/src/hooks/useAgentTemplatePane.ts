/**
 * The `agent_template_pane` opt-in flag.
 *
 * The agent editor's Agent Template pane holds only the binding dropdown, so the
 * definition it names — system prompt, skills, tools, MCP servers, guardrails —
 * is readable only on the separate Agent Templates tab. Behind this flag the
 * pane also renders that definition inline, which is the step that lets the
 * standalone tab be retired.
 *
 * Held behind a flag because it changes where a shipped surface's content lives:
 * set `agent_template_pane: true` in the running instance's
 * `$KIROCREW_HOME/config.json`. Config is read live, so no gateway restart is
 * needed.
 *
 * Same shape as `useConnectionsUi` on purpose — one predicate, one
 * `['kirocrewConfig']` cache entry, so every surface gets the same answer.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

const AGENT_TEMPLATE_PANE_FLAG = 'agent_template_pane'

/** Absent config, a failed fetch, and truthy-but-not-`true` all resolve to false. */
export function agentTemplatePaneEnabled(config: unknown): boolean {
  return (config as Record<string, unknown> | undefined)?.[AGENT_TEMPLATE_PANE_FLAG] === true
}

/** Live flag value, off the shared `['kirocrewConfig']` query cache. */
export function useAgentTemplatePaneEnabled(): boolean {
  const { data } = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  return agentTemplatePaneEnabled(data)
}
