/**
 * Shared fixtures for KAS run-view tests: a run row shaped like the router's
 * `GET /api/workflows/kas/state` payload, with every field filled so a test
 * overrides only what it is about.
 */
import type {
  KasApproval,
  KasNodeAction,
  KasQuestion,
  KasRunControls,
  KasRunNode,
  KasRunRow,
  KasStepTranscript,
} from '../api/client'

export function kasControls(overrides: Partial<KasRunControls> = {}): KasRunControls {
  return {
    display: 'running',
    primary: 'pause',
    canStop: true,
    canRetry: false,
    canDelete: false,
    needsAttention: false,
    ...overrides,
  }
}

export function kasRow(overrides: Partial<KasRunRow> = {}): KasRunRow {
  return {
    workflowId: 'wf-1',
    name: 'Review and fix',
    workflowName: 'review-fix',
    status: 'running',
    parentSessionId: 'sess-parent',
    createdAt: '2026-10-16T10:00:00Z',
    startedAt: '2026-10-16T10:00:01Z',
    endedAt: null,
    updatedAt: '2026-10-16T10:05:00Z',
    pausePending: false,
    pauseReason: null,
    completedSteps: 1,
    totalSteps: 3,
    activeSteps: [{ nodeId: 'review', label: 'Review', sessionId: 's-review' }],
    attention: [],
    blocking: null,
    controls: kasControls(),
    nodes: [
      {
        id: 'plan',
        path: 'plan',
        type: 'step',
        agent: 'planner',
        label: 'Plan',
        maxIterations: null,
        status: 'completed',
        sessionId: 's-plan',
        iteration: null,
        reason: null,
        children: [],
      },
      {
        id: 'loop',
        path: 'loop',
        type: 'repeat',
        agent: null,
        label: 'Fix loop',
        maxIterations: 3,
        status: 'running',
        sessionId: null,
        iteration: 1,
        reason: null,
        children: [
          {
            id: 'review',
            path: 'loop/review',
            type: 'step',
            agent: 'reviewer',
            label: 'Review',
            maxIterations: null,
            status: 'running',
            sessionId: 's-review',
            iteration: null,
            reason: null,
            children: [],
          },
          {
            id: 'fix',
            path: 'loop/fix',
            type: 'step',
            agent: 'fixer',
            label: 'Fix',
            maxIterations: null,
            status: 'pending',
            sessionId: null,
            iteration: null,
            reason: null,
            children: [],
          },
        ],
      },
    ],
    queuedSteps: [],
    revision: 7,
    slotKey: 'chat-abc',
    driven: true,
    ...overrides,
  }
}

export function kasApproval(overrides: Partial<KasApproval> = {}): KasApproval {
  return {
    key: 'ap-1',
    workflowId: 'wf-1',
    nodeId: 'review',
    title: 'Run shell command',
    command: 'npm test -- --run',
    options: [
      { optionId: 'o-allow', kind: 'allow_once', name: 'Allow once' },
      { optionId: 'o-always', kind: 'allow_always', name: 'Always allow' },
      { optionId: 'o-reject', kind: 'reject_once', name: 'Reject' },
    ],
    plan: null,
    isPlan: false,
    planText: null,
    createdAt: 1_760_000_000,
    ...overrides,
  }
}

/** A step's own chat, shaped like `GET …/steps/{nodeId}/transcript`. */
export function kasTranscript(overrides: Partial<KasStepTranscript> = {}): KasStepTranscript {
  return {
    workflowId: 'wf-1',
    nodeId: 'review',
    sessionId: 's-review',
    title: 'Review',
    agent: 'reviewer',
    model: 'claude',
    status: 'running',
    credits: 0.23,
    startedAt: '2026-10-16T10:01:00Z',
    updatedAt: '2026-10-16T10:02:05Z',
    entries: [
      { kind: 'user', id: 'u1', ts: null, text: 'Review the diff for bugs.' },
      { kind: 'assistant', id: 'r1', ts: null, text: 'Let me look at the changed files first.', reasoning: true },
      {
        kind: 'tool',
        id: 't1',
        ts: null,
        toolCallId: 'tc-1',
        name: 'execute_bash',
        // KAS's own title for every shell call; the row shows the command.
        title: 'Run Command',
        toolKind: 'execute',
        status: 'completed',
        input: 'git diff --stat',
        output: ' src/a.ts | 4 ++--\n 1 file changed',
      },
      { kind: 'assistant', id: 'a1', ts: null, text: 'Found **one** issue in `src/a.ts`.', reasoning: false },
    ],
    truncated: false,
    ...overrides,
  }
}

function loopStep(
  path: string,
  overrides: Partial<KasRunNode> & Pick<KasRunNode, 'id' | 'label'>,
): KasRunNode {
  return {
    type: 'step',
    agent: null,
    maxIterations: null,
    status: 'pending',
    sessionId: null,
    iteration: null,
    reason: null,
    children: [],
    ...overrides,
    path,
  }
}

/**
 * A run whose `Fix loop` repeat has reached TWO iterations: iteration 1 finished
 * (both body steps completed), iteration 2 is mid-flight (Review running, Fix
 * pending). The active instance is iteration 2's Review (`s-review-1`).
 */
export function kasLoopRow(overrides: Partial<KasRunRow> = {}): KasRunRow {
  return kasRow({
    activeSteps: [{ nodeId: 'review', label: 'Review', sessionId: 's-review-1' }],
    completedSteps: 3,
    totalSteps: 5,
    nodes: [
      loopStep('plan', { id: 'plan', label: 'Plan', agent: 'planner', status: 'completed', sessionId: 's-plan' }),
      {
        id: 'loop',
        path: 'loop',
        type: 'repeat',
        agent: null,
        label: 'Fix loop',
        maxIterations: 3,
        status: 'running',
        sessionId: null,
        iteration: 2,
        reason: null,
        // Definition / latest-instance state (rendered only when no iterations).
        children: [
          loopStep('loop/review', { id: 'review', label: 'Review', agent: 'reviewer', status: 'running', sessionId: 's-review-1' }),
          loopStep('loop/fix', { id: 'fix', label: 'Fix', agent: 'fixer', status: 'pending' }),
        ],
        iterations: [
          {
            iteration: 0,
            path: 'loop/iter-0',
            status: 'completed',
            children: [
              loopStep('loop/iter-0/review', { id: 'review', label: 'Review', agent: 'reviewer', status: 'completed', sessionId: 's-review-0' }),
              loopStep('loop/iter-0/fix', { id: 'fix', label: 'Fix', agent: 'fixer', status: 'completed', sessionId: 's-fix-0' }),
            ],
          },
          {
            iteration: 1,
            path: 'loop/iter-1',
            status: 'running',
            children: [
              loopStep('loop/iter-1/review', { id: 'review', label: 'Review', agent: 'reviewer', status: 'running', sessionId: 's-review-1' }),
              loopStep('loop/iter-1/fix', { id: 'fix', label: 'Fix', agent: 'fixer', status: 'pending' }),
            ],
          },
        ],
      },
    ],
    ...overrides,
  })
}

// ── Parity fixtures (run.pause, run.actions, node.actions, include, questions) ──

/** A run parked because its `Fix loop` used all 3 of its passes. */
export function kasLoopExhaustedRow(overrides: Partial<KasRunRow> = {}): KasRunRow {
  const base = kasLoopRow()
  const done = (n: KasRunNode): KasRunNode => ({ ...n, status: 'completed', children: n.children.map(done) })
  const nodes = base.nodes.map(n => {
    if (n.id !== 'loop') return n
    const its = (n.iterations ?? []).map(it => ({ ...it, status: 'completed', children: it.children.map(done) }))
    const last = its[its.length - 1]
    const third = last
      ? {
          ...last,
          iteration: its.length,
          path: last.path.replace(/\d+$/, String(its.length)),
          children: last.children.map(c => ({ ...c, path: c.path.replace(/iter-\d+/, `iter-${its.length}`) })),
        }
      : null
    return {
      ...n,
      status: 'paused',
      loop: { done: 3, max: 3 },
      actions: ['grant', 'complete'] as KasNodeAction[],
      iterations: third ? [...its, third] : its,
    }
  })
  return kasRow({
    ...base,
    workflowId: 'wf-loop',
    name: 'Fix until green',
    status: 'paused',
    activeSteps: [],
    controls: kasControls({ display: 'paused', primary: 'resume', needsAttention: true }),
    nodes,
    pause: { reason: 'loop_exhausted', nodeId: 'loop', path: 'loop', iterations: { done: 3, max: 3 }, message: null },
    // As `wire.run_actions` derives it: no plain Resume on an exhausted loop.
    actions: ['cancel'],
    ...overrides,
  })
}

/** Rewrite every node of a tree (children and iterations included). */
export function mapKasNodes(nodes: readonly KasRunNode[], fn: (node: KasRunNode) => KasRunNode): KasRunNode[] {
  return nodes.map(n =>
    fn({
      ...n,
      children: mapKasNodes(n.children, fn),
      ...(n.iterations ? { iterations: n.iterations.map(it => ({ ...it, children: mapKasNodes(it.children, fn) })) } : {}),
    }),
  )
}

/** A finished run whose Review step failed: Retry and Run again are offered. */
export function kasFailedRow(overrides: Partial<KasRunRow> = {}): KasRunRow {
  const nodes = mapKasNodes(kasRow().nodes, n =>
    n.id === 'review'
      ? { ...n, status: 'failed', reason: 'Tests still failing' }
      : n.id === 'fix'
        ? { ...n, status: 'skipped' }
        : n.id === 'loop'
          ? { ...n, status: 'failed' }
          : n,
  )
  return kasRow({
    workflowId: 'wf-failed',
    name: 'Nightly review',
    status: 'failed',
    endedAt: '2026-10-16T10:09:00Z',
    activeSteps: [],
    controls: kasControls({ display: 'failed', primary: null, canStop: false, canDelete: true }),
    nodes,
    actions: ['retry', 'restart', 'delete'],
    ...overrides,
  })
}

/** A run whose Review step paused for a reply: reply, Mark done and Mark failed. */
export function kasAwaitingRow(overrides: Partial<KasRunRow> = {}): KasRunRow {
  const nodes = mapKasNodes(kasRow().nodes, n =>
    n.id === 'review'
      ? { ...n, status: 'paused', actions: ['reply', 'complete', 'fail'] as KasNodeAction[] }
      : n.id === 'loop'
        ? { ...n, status: 'paused' }
        : n,
  )
  return kasRow({
    workflowId: 'wf-await',
    name: 'Review and fix',
    status: 'paused',
    activeSteps: [],
    controls: kasControls({ display: 'paused', primary: 'resume', needsAttention: true }),
    nodes,
    pause: { reason: 'awaiting_input', nodeId: 'review', path: 'loop/review', message: null },
    actions: ['resume', 'cancel'],
    ...overrides,
  })
}

/** A running run that included `code-review`: its group and two prefixed members. */
export function kasIncludeRow(overrides: Partial<KasRunRow> = {}): KasRunRow {
  const plan = kasRow().nodes[0]
  return kasRow({
    workflowId: 'wf-include',
    name: 'Ship the fix',
    activeSteps: [{ nodeId: 'review/test', label: 'review/Test', sessionId: 's-test' }],
    completedSteps: 2,
    totalSteps: 4,
    nodes: [
      plan,
      {
        ...plan,
        id: 'review',
        path: 'review',
        type: 'sequence',
        agent: null,
        label: 'review',
        status: 'running',
        sessionId: null,
        include: { recipe: 'code-review', path: '.kiro/workflows/code-review.yaml' },
        children: [
          { ...plan, id: 'review/lint', path: 'review/lint', label: 'review/Lint', agent: 'linter', status: 'completed', sessionId: 's-lint' },
          {
            ...plan,
            id: 'review/test',
            path: 'review/test',
            label: 'review/Test',
            agent: 'tester',
            status: 'running',
            sessionId: 's-test',
            actions: ['cancel'],
          },
        ],
      },
      { ...plan, id: 'ship', path: 'ship', label: 'Ship', agent: 'shipper', status: 'pending', sessionId: null },
    ],
    actions: ['pause', 'cancel'],
    ...overrides,
  })
}

/** An open question-tool ask from wf-1's Review step: one single-choice + free text, one multi-select. */
export function kasQuestion(overrides: Partial<KasQuestion> = {}): KasQuestion {
  return {
    key: 'q-1',
    workflowId: 'wf-1',
    nodeId: 'review',
    path: 'loop/review',
    slot: 'chat-abc',
    title: null,
    questions: [
      {
        id: 'scope',
        prompt: 'Which findings should I fix?',
        options: [{ label: 'Only the failing tests' }, { label: 'Everything flagged', description: 'Includes the style nits' }],
        multiSelect: false,
        freeText: true,
      },
      {
        id: 'checks',
        prompt: 'Which checks should run afterwards?',
        options: [{ label: 'Lint' }, { label: 'Unit tests' }, { label: 'Typecheck' }],
        multiSelect: true,
      },
    ],
    ...overrides,
  }
}

/** A plan approval carrying KAS's structured plan as `plan.nodes`. */
export function kasPlanApproval(overrides: Partial<KasApproval> = {}): KasApproval {
  const planText = 'plan: Plan\nloop: Fix loop (max 3)\n  review: Review\n  fix: Fix'
  return kasApproval({
    key: 'ap-plan',
    title: 'Approve the workflow plan',
    command: null,
    isPlan: true,
    planText,
    options: [
      { optionId: 'approve', kind: 'allow_once', name: 'Approve' },
      { optionId: 'reject', kind: 'reject_once', name: 'Reject' },
    ],
    plan: {
      text: planText,
      nodes: [
        { id: 'plan', label: 'Plan', agent: 'planner' },
        {
          id: 'loop',
          label: 'Fix loop',
          type: 'repeat',
          maxIterations: 3,
          children: [
            { id: 'review', label: 'Review', agent: 'reviewer' },
            { id: 'fix', label: 'Fix', agent: 'fixer' },
          ],
        },
        { id: 'docs', label: 'docs', include: { recipe: 'update-docs', path: '.kiro/workflows/update-docs.yaml' }, children: [{ id: 'docs/write', label: 'docs/Write' }] },
      ],
    },
    ...overrides,
  })
}
