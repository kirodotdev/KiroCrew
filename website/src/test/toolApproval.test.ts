import { describe, it, expect } from 'vitest'
import type { PendingDecision, ToolInvocationState } from '../types/toolApproval'
import type { PendingApproval } from '../types/index'

// Task 0 is a types + seam-confirmation task. These tests lock in:
//   1. `PendingDecision` wraps the REAL `PendingApproval` event (no new wire
//      field); if that wire shape drifts, `tsc` fails here.
//   2. The resume identifier is `approval.request_id` — the id-scoped
//      `api.resolveApproval` path, not a fabricated one.
//   3. `ToolInvocationState` models the three reachable lifecycle phases, with
//      `input-available` as the approvable moment.

describe('PendingDecision model', () => {
  it('wraps the real PendingApproval event (no new wire field)', () => {
    // Typed as the existing wire shape — the compiler enforces the field set.
    const approval: PendingApproval = {
      tool: 'fs_write',
      tool_input: '{"path":"/tmp/x","content":"hi"}',
      tool_kind: 'edit',
      request_id: 'req-123',
    }

    const pending: PendingDecision = {
      slot: 'chat-1',
      approval,
      invocation: { state: 'input-available', input: JSON.parse(approval.tool_input) },
    }

    expect(pending.slot).toBe('chat-1')
    // The resume identifier is request_id (id-scoped api.resolveApproval).
    expect(pending.approval.request_id).toBe('req-123')
    // The raw-input source is the verbatim event field.
    expect(pending.approval.tool_input).toBe(approval.tool_input)
    expect(pending.toolCallId).toBeUndefined()
    expect(pending.invocation.state).toBe('input-available')
  })

  it('carries an optional tool_call_id for display/correlation when present', () => {
    const approval: PendingApproval = {
      tool: 'grep',
      tool_input: '{"pattern":"foo"}',
      tool_kind: 'read',
      request_id: 'req-9',
    }
    const pending: PendingDecision = {
      slot: 'chat-1',
      approval,
      toolCallId: 'tc-abc',
      invocation: { state: 'input-available', input: { pattern: 'foo' } },
    }
    expect(pending.toolCallId).toBe('tc-abc')
    // Even with a tool_call_id present, request_id remains the resume id.
    expect(pending.approval.request_id).toBe('req-9')
  })

  it('models the three reachable lifecycle phases', () => {
    const samples: ToolInvocationState[] = [
      { state: 'input-available', input: {} },
      { state: 'output-available', input: {}, output: {} },
      { state: 'output-error', input: {}, error: 'TOOL_DENY' },
    ]
    expect(samples.map((s) => s.state)).toEqual([
      'input-available',
      'output-available',
      'output-error',
    ])
    const errored = samples[2]
    if (errored.state === 'output-error') expect(errored.error).toBe('TOOL_DENY')
  })
})
