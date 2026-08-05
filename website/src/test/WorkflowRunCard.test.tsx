import { describe, it, expect } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import WorkflowRunCard, { extractWorkflowRunId, isWorkflowRunTool } from '../pages/chat/WorkflowRunCard'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

const RUN_ID = 'wf_000042'

function wfToolMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    role: 'tool',
    content: '🔧 workflow_run',
    cls: '',
    meta: {
      tool_call_id: 'tc_wf',
      input: JSON.stringify({ intent: 'deep dive to find bugs' }),
      output: `Started workflow run \`${RUN_ID}\`. It runs in the background — monitor with workflow_status.`,
    },
    ...overrides,
  }
}

describe('WorkflowRunCard detection helpers', () => {
  it('extracts the run id from a workflow_run tool result', () => {
    expect(extractWorkflowRunId(wfToolMsg())).toBe(RUN_ID)
  })

  it('returns null for a non-workflow tool message', () => {
    const msg: ChatMessage = { role: 'tool', content: '🔧 Running: echo hi', cls: '', meta: { output: 'hi' } }
    expect(extractWorkflowRunId(msg)).toBeNull()
    expect(isWorkflowRunTool(msg)).toBe(false)
  })

  it('returns null when the launch output has not arrived yet', () => {
    expect(extractWorkflowRunId(wfToolMsg({ meta: { tool_call_id: 'tc_wf' } }))).toBeNull()
  })

  it('isWorkflowRunTool is true only for tool-role workflow launches', () => {
    expect(isWorkflowRunTool(wfToolMsg())).toBe(true)
    // Same output text on a non-tool role must not qualify.
    expect(isWorkflowRunTool(wfToolMsg({ role: 'assistant' }))).toBe(false)
  })
})

describe('WorkflowRunCard rendering', () => {
  it('shows live status/phase from the workflowRuns slice', () => {
    const store = createTestStore({
      chat: {
        workflowRuns: {
          [RUN_ID]: {
            run_id: RUN_ID,
            name: 'Deep Dive Bug Hunt',
            phase: 'map-codebase',
            lastLog: 'Mapping the codebase structure',
            status: 'running',
          },
        },
      } as unknown as ChatState,
    })
    renderWithProviders(<WorkflowRunCard runId={RUN_ID} message={wfToolMsg()} />, { store })
    expect(screen.getByText('Deep Dive Bug Hunt')).toBeTruthy()
    expect(screen.getByText('map-codebase')).toBeTruthy()
    expect(screen.getByText('Mapping the codebase structure')).toBeTruthy()
  })

  it('falls back to the launch intent when no live run is present', () => {
    const store = createTestStore({ chat: { workflowRuns: {} } as unknown as ChatState })
    renderWithProviders(<WorkflowRunCard runId={RUN_ID} message={wfToolMsg()} />, { store })
    expect(screen.getByText('deep dive to find bugs')).toBeTruthy()
  })

  it('clicking the card opens the Workflows side panel', () => {
    const store = createTestStore({ chat: { workflowRuns: {} } as unknown as ChatState })
    renderWithProviders(<WorkflowRunCard runId={RUN_ID} message={wfToolMsg()} />, { store })
    fireEvent.click(screen.getByRole('button'))
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(store.getState().chat.activityTab).toBe('workflows')
  })
})
