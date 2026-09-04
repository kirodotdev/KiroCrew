// Payload contract for GET /api/skills (api.skills in api/client.ts).
//
// The endpoint answers with the bare array (legacy, every unscoped caller) OR
// — only when the server actually applied the agent's skill:// mapping — the
// scoped envelope. The envelope is the only way to tell a filtered list
// (especially an EMPTY one: "no skills mapped to this agent") apart from the
// unfiltered catalog ("no skills exist"): the arrays themselves are
// byte-identical. Every consumer that passes `agent` MUST unwrap through
// unwrapSkills() instead of assuming an array shape.
//
// Lives beside the other picker helpers (not in api/client.ts) so tests that
// stub the api client with a partial factory keep this normalization live.

export interface AgentScopedSkills<T> {
  skills: T[]
  agent_scoped?: boolean
  agent?: string
}

export type SkillsPayload<T> = T[] | AgentScopedSkills<T>

/** Normalize either /api/skills payload shape. `agentScoped` is server truth
 *  ("the agent's mapping was applied"), never inferred by the caller: an
 *  agent with no mapping of its own gets the legacy bare array and must
 *  render with zero scope cues. A malformed envelope flagging the scope
 *  WITHOUT naming the agent is treated as unscoped — scoped copy
 *  interpolates the name, and "Scoped to agent " is worse than no cue. */
export function unwrapSkills<T>(
  payload: SkillsPayload<T> | undefined,
): { items: T[]; agentScoped: boolean; scopedAgent: string } {
  if (Array.isArray(payload)) return { items: payload, agentScoped: false, scopedAgent: '' }
  if (payload && Array.isArray(payload.skills)) {
    const scopedAgent = typeof payload.agent === 'string' ? payload.agent : ''
    return {
      items: payload.skills,
      agentScoped: payload.agent_scoped === true && scopedAgent !== '',
      scopedAgent,
    }
  }
  return { items: [], agentScoped: false, scopedAgent: '' }
}
