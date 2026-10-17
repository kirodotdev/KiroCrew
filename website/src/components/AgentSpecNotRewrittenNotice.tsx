/**
 * The notice a dashboard edit shows when its change was saved but the agent-spec
 * rebuild did not confirm a rewrite (the backend answers `agent_spec_rewritten:
 * false`, or `rebuild.ok: false` from MCP apply).
 *
 * That is the outcome of a step that failed (the rebuild declined to write or
 * raised), so it renders through `ErrorNotice` with the agent hand-off where the
 * hand-off cannot discard anything: it opens a chat, and the gateway log it
 * points at is something the agent can read. Every surface that reports the flag renders this one component
 * so the wording stays the same.
 */
import ErrorNotice from './ErrorNotice'
import { i18nT } from '../i18n/t'

/** True when a response says the save landed but the agent spec did not follow. */
export function agentSpecNotRewritten(body: { agent_spec_rewritten?: boolean } | null | undefined): boolean {
  return body?.agent_spec_rewritten === false
}

export default function AgentSpecNotRewrittenNotice({
  className = '',
  askAgent = true,
}: {
  className?: string
  /** Off where the hand-off would navigate away from an unsaved edit. */
  askAgent?: boolean
}) {
  return (
    <ErrorNotice
      variant="inline"
      className={`text-xs ${className}`}
      message={i18nT('components.agentSpecNotRewrittenNotice.saved_but_not_applied')}
      askAgent={askAgent}
      testId="agent-spec-not-rewritten"
    />
  )
}
