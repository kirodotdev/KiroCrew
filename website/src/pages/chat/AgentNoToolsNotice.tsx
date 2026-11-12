import { memo } from 'react'
import { useInRouterContext, useNavigate } from 'react-router-dom'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { agentTemplateHref } from '../overview/agentTemplateLink'
import NoticeCard from './NoticeCard'

/** The link after the notice's copy: opens the agent's page, where its tools are chosen. */
function OpenAgentLink({ agent }: { agent: string }) {
  const navigate = useNavigate()
  return (
    <button
      type="button"
      data-testid="agent-no-tools-open"
      className="whitespace-nowrap underline underline-offset-2 hover:text-text focus-visible:text-text"
      onClick={() => navigate(agentTemplateHref(agent))}
    >
      {i18nT('pages.chat.gatewayNotice.agent_no_tools_open')}
    </button>
  )
}

/**
 * The notice that the chat's agent lists no tools, with a link to that agent's
 * page under Customize. `content` is the localized copy
 * (`gatewayNotice.ts`). A host with no router (an app's embedded chat) draws the
 * copy alone: it has no Customize page to open.
 */
export default memo(function AgentNoToolsNotice({ content, agent }: { content: string; agent: string }) {
  useLanguageGeneration()
  const routed = useInRouterContext()
  return <NoticeCard content={content} tone="info" action={routed ? <OpenAgentLink agent={agent} /> : undefined} />
})
