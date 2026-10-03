/**
 * The built-in Assistant crewmate.
 *
 * The Assistant is its own built-in crew member (config key `assistant`, kiro
 * template `kirocrew-assistant`), separate from the reserved `default` member,
 * which keeps its ordinary presentation. BOTH halves identify it: a user's own
 * member that merely happens to be named `assistant` on another template is an
 * ordinary crewmate and gets none of the Assistant's behaviour.
 */

export const ASSISTANT_MEMBER_NAME = 'assistant'
export const ASSISTANT_KIRO_AGENT = 'kirocrew-assistant'

/** True only for the built-in Assistant: name `assistant` AND its template. */
export function isAssistantMember(m: { name?: string; kiro_agent?: unknown } | null | undefined): boolean {
  return !!m && m.name === ASSISTANT_MEMBER_NAME && m.kiro_agent === ASSISTANT_KIRO_AGENT
}
