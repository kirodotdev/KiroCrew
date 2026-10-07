import ChatInput from '../../components/ChatInput'
import type { ChatInputProps } from '../../components/chat-input/props'

/** The session chrome the ordinary chat composer draws: the agent, model and
 *  effort chips, the context-window meter, and the approval-mode picker. A
 *  crewmate sets its permission, model and effort on its profile card instead,
 *  so none of these reach the Crewmates page's composer. */
export const SESSION_CHROME_PROPS = [
  'onAgentClick', 'agentLabel', 'agentIsInheritedDefault', 'agentSource',
  'onModelClick', 'modelName', 'modelIsInheritedDefault', 'modelIsAutoChosen', 'modelIsJevRouted',
  'reasoningEffort', 'effortIsDefault', 'hasEffort',
  'contextPct', 'contextUsedTokens', 'contextWindowTokens',
  'approvalMode',
] as const satisfies ReadonlyArray<keyof ChatInputProps>

export type CrewComposerProps = Omit<ChatInputProps, (typeof SESSION_CHROME_PROPS)[number]>

/**
 * The Crewmates page's composer: a DM with one crewmate, so it is the text
 * input, attachments, send / stop / steer and the queue, with no toolbar line.
 * It reuses the shared ChatInput for every one of those pieces rather than a
 * copy; only the session chrome is left out. ChatPane renders it through its
 * `composerInput` slot, so the ordinary chat composer is untouched.
 */
function CrewComposer(props: ChatInputProps) {
  const core: Record<string, unknown> = { ...props }
  for (const key of SESSION_CHROME_PROPS) delete core[key]
  return <ChatInput {...(core as CrewComposerProps)} />
}

// Not memo(): ChatInput is memo()'d itself and subscribes to the language,
// so a second boundary here would only hide a language switch.
export default CrewComposer
