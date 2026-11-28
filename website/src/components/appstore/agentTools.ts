/** One `agentTools` grant entry as an app row carries it: the tool and the entry to approve. */
export interface AgentToolGrant {
  name: string
  description: string
  /** `<tool name>@<digest>`: what the approval sends back, never shown. */
  entry: string
}

/** One tool a registry row's `agentTools` block discloses, as the install card lists it. */
export interface DeclaredAgentTool {
  name: string
  description: string
  /** `<tool name>@<digest>`: sent back with the install, which approves only these. */
  entry: string
}

/**
 * The tools a disclosed `agentTools` block lists, each with a string name and the
 * grant entry the gateway computed. A tool without an entry lists nothing: the
 * card shows only what the install it confirms will approve.
 */
export function declaredAgentTools(block: unknown): DeclaredAgentTool[] {
  const tools = (block as { tools?: unknown } | null | undefined)?.tools
  if (!Array.isArray(tools)) return []
  return tools.flatMap(tool => {
    const { name, description, entry } = (tool ?? {}) as { name?: unknown; description?: unknown; entry?: unknown }
    return typeof name === 'string' && name && typeof entry === 'string' && entry
      ? [{ name, description: typeof description === 'string' ? description : '', entry }]
      : []
  })
}

/** The entries an install card listed for *block*: the `agentToolsConsent` it sends. */
export function agentToolEntries(block: unknown): string[] {
  return declaredAgentTools(block).map(tool => tool.entry)
}
