/**
 * Fixtures and queries shared by AgentBackendTab.test.tsx and
 * AgentBackendTab.detailCard.test.tsx: builders for the schema map, a probe row and
 * its card payload, the render helper, and queries for the harness rows, the Use
 * button and the open detail.
 */
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AgentBackendTab } from '../pages/developer/AgentBackendTab'

/** A schema map advertising exactly `values` for the backend field. */
export function schemaWith(values: string[] | undefined) {
  if (!values) return undefined
  return new Map([['agent.acp_backend', { path: 'agent.acp_backend', type: 'enum', enum: values }]])
}

/** A card payload shaped like the server's, defaulted to the dull answer. */
export function card(
  over: Partial<{
    capabilities: {
      id: string
      available: boolean
      measured?: boolean
      unmeasured_reason?: string
    }[]
    details: { id: string; available: boolean; measured?: boolean }[]
    security_notes: string[]
    operator_notes: string[]
    tool_approval: string
    offered_by_build: boolean
    mcp: {
      per_tool_deny: string
      costs_whole_server?: boolean
      ineffective: string[]
    }
  }> = {},
) {
  return {
    capabilities: [
      { id: 'crew_tools', available: true },
      { id: 'mid_turn_steer', available: false },
    ],
    security_notes: [],
    operator_notes: [],
    tool_approval: 'agent_spec',
    offered_by_build: true,
    ...over,
  }
}

/**
 * One `GET /api/acp-backends` row, defaulted to the uninteresting answer
 * (selectable and installed) so each test states only the field it is about.
 */
export function probeRow(
  id: string,
  over: Partial<{
    selectable: boolean
    installed: string
    missing_components: string[]
    install_command: string
    found_version: string
    restart_required: boolean
    auth: { sign_in_remedy: string; signs_in_separately: boolean }
    capabilities: {
      id: string
      available: boolean
      measured?: boolean
      unmeasured_reason?: string
    }[]
    security_notes: string[]
    operator_notes: string[]
    tool_approval: string
    offered_by_build: boolean
    mcp: {
      per_tool_deny: string
      costs_whole_server?: boolean
      ineffective: string[]
    }
  }> = {},
) {
  return {
    id,
    policy_id: id || 'kiro',
    selectable: true,
    installed: 'installed',
    missing_components: [],
    install_command: '',
    restart_required: false,
    // No `auth` by default: an older gateway sends none, so the uninteresting row
    // is the one that carries no auth object at all.
    ...over,
  }
}

export function wrapWithClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AgentBackendTab />
    </QueryClientProvider>,
  )
  return { qc }
}

export function wrap() {
  wrapWithClient()
}

/**
 * A harness ROW's radio. Its accessible name is the harness name alone, so it can
 * never be confused with the Use button, and `checked` is which detail is open.
 */
export const row = (name: string) => screen.getByRole('radio', { name })
/** The one control that switches the backend. */
export const useButton = (name: string) => screen.getByRole('button', { name: `Use ${name}` })
/** Put a harness's detail on screen without switching to it. */
export const highlight = (name: string) => fireEvent.click(row(name))
/** The one open detail: the panel under the checked row. */
export const detail = () => screen.getByTestId('agent-harness-detail')
/**
 * Queries scoped to the open detail.
 *
 * Load-bearing, not tidiness. Each harness's status sentence is in the DOM TWICE by
 * design -- once as the row's `aria-describedby` text, so a reader arrowing down the
 * list is told each state, and once visibly in the detail. An unscoped `getByText`
 * cannot choose between them, and the assertion worth making is the scoped one: that
 * the DETAIL says it, rather than that the document contains it somewhere.
 */
export const panel = () => within(detail())
