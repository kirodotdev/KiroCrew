/**
 * The address of one custom agent's page: the Custom agents tab under Customize,
 * opened on that agent. `AgentTemplatesTab` reads the parameter; a link built
 * anywhere else (a chat notice) goes through here so the two cannot drift.
 */
export const TEMPLATE_PARAM = 'template'

export function agentTemplateHref(name: string): string {
  return `/capabilities?tab=templates&${TEMPLATE_PARAM}=${encodeURIComponent(name)}`
}
