import { createContext } from 'react'

/**
 * Self-hosted Jira hosts for markdown link chips.
 *
 * Atlassian Cloud (`*.atlassian.net`) URLs chip with NO provider — the suffix
 * identifies the product on its own. Only self-hosted / Data Center instances
 * need this list, and it comes from the operator's `dashboard.jira_hosts`
 * config, never from message content.
 *
 * Lives in its own module (not MarkdownRenderer) on purpose: many tests mock
 * MarkdownRenderer down to a bare default export, and consumers of the context
 * (ChatPage) must not couple their imports to that module's export surface.
 */
export const JiraHostsCtx = createContext<readonly string[]>([])
