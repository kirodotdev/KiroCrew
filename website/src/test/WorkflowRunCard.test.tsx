import { beforeEach, describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

const mocks = vi.hoisted(() => ({
  promoteWorkflowRun: vi.fn(),
  snapshot: {
    snapshot: null as null | {
      run_id: string
      name: string
      status: string
      source: string
      events: never[]
    },
    loading: false,
    error: null as string | null,
    refresh: vi.fn(),
  },
}))

vi.mock('../api/client', () => ({
  api: { promoteWorkflowRun: mocks.promoteWorkflowRun },
}))
vi.mock('../apps/workflows/useRunSnapshot', () => ({
  useRunSnapshot: () => mocks.snapshot,
}))
vi.mock('../apps/workflows/WorkflowSourceCode', () => ({
  default: ({ source, ariaLabel }: { source: string; ariaLabel: string }) => (
    <pre aria-label={ariaLabel} data-language="python" data-line-numbers="true">
      {source}
    </pre>
  ),
}))
import { renderWithProviders, createTestStore } from './helpers'
import WorkflowRunCard, {
  extractWorkflowRunId,
  isWorkflowRunTool,
} from '../pages/chat/WorkflowRunCard'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

const RUN_ID = 'wf_000042'
const SOURCE =
  'META = {"name": "Deep Dive Bug Hunt", "description": "Investigate failures"}\n' +
  'async def workflow(ctx):\n    return {"ok": True}\n'

beforeEach(() => {
  mocks.promoteWorkflowRun.mockReset()
  mocks.snapshot.snapshot = null
  mocks.snapshot.loading = false
  mocks.snapshot.error = null
  mocks.snapshot.refresh.mockReset()
})

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
    const msg: ChatMessage = {
      role: 'tool',
      content: '🔧 Running: echo hi',
      cls: '',
      meta: { output: 'hi' },
    }
    expect(extractWorkflowRunId(msg)).toBeNull()
    expect(isWorkflowRunTool(msg)).toBe(false)
  })

  it('returns null when the launch output has not arrived yet', () => {
    expect(
      extractWorkflowRunId(wfToolMsg({ meta: { tool_call_id: 'tc_wf' } })),
    ).toBeNull()
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
    renderWithProviders(
      <WorkflowRunCard runId={RUN_ID} message={wfToolMsg()} />,
      { store },
    )
    expect(screen.getByText('Deep Dive Bug Hunt')).toBeTruthy()
    expect(screen.getByText('map-codebase')).toBeTruthy()
    expect(screen.getByText('Mapping the codebase structure')).toBeTruthy()
  })

  it('falls back to the launch intent when no live run is present', () => {
    const store = createTestStore({
      chat: { workflowRuns: {} } as unknown as ChatState,
    })
    renderWithProviders(
      <WorkflowRunCard runId={RUN_ID} message={wfToolMsg()} />,
      { store },
    )
    expect(screen.getByText('deep dive to find bugs')).toBeTruthy()
  })

  it('clicking the card opens the Workflows side panel', () => {
    const store = createTestStore({
      chat: { workflowRuns: {} } as unknown as ChatState,
    })
    renderWithProviders(
      <WorkflowRunCard runId={RUN_ID} message={wfToolMsg()} />,
      { store },
    )
    fireEvent.click(screen.getByRole('button'))
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(store.getState().chat.activityTab).toBe('workflows')
  })

  it('offers to save only a successful ad-hoc workflow run', () => {
    const finishedStore = createTestStore({
      chat: {
        workflowRuns: {
          [RUN_ID]: {
            run_id: RUN_ID,
            name: 'Deep Dive Bug Hunt',
            status: 'finished',
          },
        },
      } as unknown as ChatState,
    })
    const { container, unmount } = renderWithProviders(
      <WorkflowRunCard runId={RUN_ID} message={wfToolMsg()} />,
      { store: finishedStore },
    )

    expect(
      screen.getByRole('button', { name: 'Save workflow' }),
    ).toBeInTheDocument()
    expect(container.querySelector('svg.text-ok')).not.toBeNull()
    unmount()

    const runningStore = createTestStore({
      chat: {
        workflowRuns: {
          [RUN_ID]: {
            run_id: RUN_ID,
            name: 'Deep Dive Bug Hunt',
            status: 'running',
          },
        },
      } as unknown as ChatState,
    })
    renderWithProviders(
      <WorkflowRunCard runId={RUN_ID} message={wfToolMsg()} />,
      {
        store: runningStore,
      },
    )
    expect(
      screen.queryByRole('button', { name: 'Save workflow' }),
    ).not.toBeInTheDocument()
  })

  it('promotes the exact completed session source and shows its slash command', async () => {
    const store = createTestStore({
      chat: {
        workflowRuns: {
          [RUN_ID]: {
            run_id: RUN_ID,
            name: 'Deep Dive Bug Hunt',
            status: 'finished',
          },
        },
      } as unknown as ChatState,
    })
    mocks.snapshot.snapshot = {
      run_id: RUN_ID,
      name: 'Deep Dive Bug Hunt',
      status: 'finished',
      source: SOURCE,
      events: [],
    }
    mocks.promoteWorkflowRun.mockResolvedValue({
      ok: true,
      definition: {
        id: 'wfd_debug',
        slug: 'deep-dive-bug-hunt',
        name: 'Deep Dive Bug Hunt',
        description: 'Investigate failures',
        revision: 1,
        source: SOURCE,
        derived_from: { workflow_id: 'wfd_parent', revision: 2 },
        revisions: [],
      },
    })
    renderWithProviders(
      <WorkflowRunCard runId={RUN_ID} message={wfToolMsg()} />,
      { store },
    )

    fireEvent.click(screen.getByRole('button', { name: 'Save workflow' }))
    expect(
      screen.getByRole('dialog', { name: 'Save this workflow' }),
    ).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveValue('Deep Dive Bug Hunt')
    expect(screen.getByLabelText('Slash-command name')).toHaveValue(
      'deep-dive-bug-hunt',
    )
    expect(screen.getByLabelText('Description')).toHaveValue(
      'Investigate failures',
    )
    expect(screen.getByLabelText('Workflow source').textContent).toBe(SOURCE)
    expect(screen.getByLabelText('Workflow source')).toHaveAttribute(
      'data-language',
      'python',
    )
    expect(screen.getByLabelText('Workflow source')).toHaveAttribute(
      'data-line-numbers',
      'true',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Save to library' }))

    await waitFor(() =>
      expect(mocks.promoteWorkflowRun).toHaveBeenCalledWith(RUN_ID, {
        name: 'Deep Dive Bug Hunt',
        description: 'Investigate failures',
        slug: 'deep-dive-bug-hunt',
      }),
    )
    expect(
      await screen.findByText('Saved as /workflow deep-dive-bug-hunt'),
    ).toBeInTheDocument()
  })

  it('locks the save fields while promotion is pending', async () => {
    const store = createTestStore({
      chat: {
        workflowRuns: {
          [RUN_ID]: {
            run_id: RUN_ID,
            name: 'Deep Dive Bug Hunt',
            status: 'finished',
          },
        },
      } as unknown as ChatState,
    })
    mocks.snapshot.snapshot = {
      run_id: RUN_ID,
      name: 'Deep Dive Bug Hunt',
      status: 'finished',
      source: SOURCE,
      events: [],
    }
    mocks.promoteWorkflowRun.mockReturnValue(new Promise(() => {}))
    renderWithProviders(
      <WorkflowRunCard runId={RUN_ID} message={wfToolMsg()} />,
      { store },
    )

    fireEvent.click(screen.getByRole('button', { name: 'Save workflow' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save to library' }))
    await waitFor(() =>
      expect(mocks.promoteWorkflowRun).toHaveBeenCalledTimes(1),
    )

    expect(screen.getByLabelText('Name')).toBeDisabled()
    expect(screen.getByLabelText('Slash-command name')).toBeDisabled()
    expect(screen.getByLabelText('Description')).toBeDisabled()
  })
})

describe('WorkflowRunCard — opening from a background pane retargets the panel first', () => {
  // Same rule as SubagentRunCard: the Workflows panel is mounted for
  // `activeSlot`, which split view never moves with pane focus.
  const msg = wfToolMsg()

  it('activates the card\u2019s own session, then opens the Workflows tab', () => {
    const store = createTestStore({
      chat: {
        activeSlot: 'chat-9',
        workflowRuns: {},
        // switchSlot.pending reads these; a partial state would throw instead.
        slotHistory: [],
        slotMessages: {},
        slotActivity: {},
        messages: [],
        toolLog: [],
        subagents: {},
      } as unknown as ChatState,
    })
    renderWithProviders(
      <WorkflowRunCard runId={RUN_ID} message={msg} slot="chat-1" />,
      { store },
    )
    fireEvent.click(screen.getByRole('button'))
    expect(store.getState().chat.activeSlot).toBe('chat-1')
    expect(store.getState().chat.activityTab).toBe('workflows')
  })

  it('does not retarget when no slot is supplied (single chat draws only the active session)', () => {
    const store = createTestStore({
      chat: { activeSlot: 'chat-1', workflowRuns: {} } as unknown as ChatState,
    })
    renderWithProviders(<WorkflowRunCard runId={RUN_ID} message={msg} />, {
      store,
    })
    fireEvent.click(screen.getByRole('button'))
    expect(store.getState().chat.activeSlot).toBe('chat-1')
    expect(store.getState().chat.activityTab).toBe('workflows')
  })

  it('keeps the affordance in a background pane rather than going quiet', () => {
    const store = createTestStore({
      chat: { activeSlot: 'chat-9', workflowRuns: {} } as unknown as ChatState,
    })
    renderWithProviders(
      <WorkflowRunCard runId={RUN_ID} message={msg} slot="chat-1" />,
      { store },
    )
    expect(screen.getByRole('button')).toBeTruthy()
    expect(screen.getByText(new RegExp(RUN_ID))).toBeTruthy()
  })
})
