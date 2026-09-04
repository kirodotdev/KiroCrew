/**
 * transcriptRenderers — what each entry actually RETURNS.
 *
 * The sibling suite (transcriptRenderers.test.tsx) pins RESOLUTION: which entry
 * claims which message, and in what order. That leaves the render bodies
 * unexercised, so a row could resolve correctly and still hand its card the
 * wrong props — or none of the host callbacks the whole module exists to wire
 * through. Here each entry is rendered and the returned element is inspected.
 *
 * `render` returns an element and never mounts it, so the store-connected cards
 * stay uninstantiated and no provider is needed.
 */
import { describe, it, expect, vi } from 'vitest'
import type { ReactElement } from 'react'
import type { ChatMessage } from '../types'
import { mergeRenderers, resolveRenderer, type MessageRenderContext, type MessageRenderer } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers, type TranscriptRendererOptions } from '../pages/chat/transcriptRenderers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import StopEventCard from '../pages/chat/StopEventCard'
import RecoveryCard from '../pages/chat/RecoveryCard'
import WorkflowRunCard from '../pages/chat/WorkflowRunCard'
import SubagentRunCard from '../pages/chat/SubagentRunCard'
import WorkflowCompletionCard from '../pages/chat/WorkflowCompletionCard'
import SubagentCompletionCard from '../pages/chat/SubagentCompletionCard'
import { ErrorCard } from '../pages/chat/ErrorCard'

const msg = (role: string, over: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role, content: '', cls: '', ...over }) as ChatMessage

/** Identity row/wrapper, but recorded, so the tool row's "wide" flag is visible. */
function ctxOf(over: Partial<MessageRenderContext> = {}) {
  const row = vi.fn((children: unknown) => children)
  const ctx = {
    index: 0,
    messages: [] as ChatMessage[],
    running: false,
    key: 'zzq-key',
    hideCardOwnedOAuth: false,
    autoDeniedIds: new Set<string>(),
    wrapper: (children: unknown) => children,
    row,
    ...over,
  } as unknown as MessageRenderContext
  return { ctx, row }
}

const OPTS: TranscriptRendererOptions = {
  slot: 'zzq-slot',
  onFileOpen: () => {},
  onFolderOpen: () => {},
  onOpenSubagentPanel: () => {},
  onToolDisclosureChange: () => {},
  toolDisclosure: { 'zzq-key': true },
  appInPanel: true,
  onOpenApp: () => {},
}

/** Render through the merged registry, the way a pane does. */
function drawn(m: ChatMessage, over?: Partial<MessageRenderContext>, opts: TranscriptRendererOptions = OPTS) {
  const { ctx, row } = ctxOf(over)
  const entry = resolveRenderer(m, mergeRenderers(createTranscriptRenderers(opts)))!
  return { el: entry.render(m, ctx) as ReactElement | null, row, id: entry.id }
}

/** Reach an entry by id, to exercise a defensive branch the match guard makes
 *  unreachable through resolution. */
const entryById = (id: string): MessageRenderer =>
  createTranscriptRenderers(OPTS).find(r => r.id === id)!

const workflowLaunch = msg('tool', {
  content: '🔧 workflow_run',
  meta: { output: 'Started workflow run `wf_abc123`' },
})
const subagentLaunch = msg('tool', {
  content: '🔧 spawn_run',
  meta: { output: 'Spawned 2 subagent(s).\n  1a2b3c4d (kirocrew): read specs\n  5e6f7a8b (kirocrew): read code' },
})

describe('the generic tool row', () => {
  it('draws the live tool line as a WIDE row, wired to the host', () => {
    const { el, row } = drawn(msg('tool', { content: '🔧 grep' }), { running: true })

    expect(el!.type).toBe(ToolCallLine)
    expect(el!.props).toMatchObject({
      slot: 'zzq-slot',
      running: true,
      disclosure: true,
      disclosureKey: 'zzq-key',
      appInPanel: true,
    })
    expect(el!.props.onFileOpen).toBe(OPTS.onFileOpen)
    expect(el!.props.onDisclosureChange).toBe(OPTS.onToolDisclosureChange)
    // Second argument to ctx.row is the wide-row flag.
    expect(row.mock.calls[0][1]).toBe(true)
  })
})

describe('the shape-matched cards', () => {
  it('draws a stop event through StopEventCard', () => {
    // Resolved from the SDK default now that this module no longer overrides the
    // row — the point being that the merged registry still lands on the card.
    const m = msg('assistant', { kind: 'stop_event' })
    const { el, id } = drawn(m)
    expect(id).toBe('stop_event')
    expect(el!.type).toBe(StopEventCard)
    expect(el!.props.message).toBe(m)
  })

  it('draws a sub-agent completion with the folder and panel affordances wired', () => {
    const m = msg('subagent', {
      content: '[Subagent completion event]\nAgent `1a2b3c4d` (kirocrew) ✅ completed\nTask: read specs\n',
    })
    const { el, id, row } = drawn(m)
    expect(id).toBe('subagent_completion')
    expect(el!.type).toBe(SubagentCompletionCard)
    expect(el!.props.onFolderOpen).toBe(OPTS.onFolderOpen)
    expect(el!.props.onOpenPanel).toBe(OPTS.onOpenSubagentPanel)
    expect(el!.props.disclosureKey).toBe('zzq-key')
    // The host owns row geometry, so this card IS wrapped. It used to be left
    // bare because the card applied `px-5` + the --mc-content-width clamp at its
    // own root; that double-padded it in ChatPage and, here, left it flush at 0px
    // in ChatPane and ChatEmbed, where nothing else supplies a gutter.
    // `true` is the tight flag: py-0.5, the density the card carried itself.
    expect(row).toHaveBeenCalledTimes(1)
    expect(row.mock.calls[0][1]).toBe(true)
  })
})

describe('the two launch cards', () => {
  it('draws a workflow launch with the extracted run id, wrapped by the host', () => {
    const { el, id, row } = drawn(workflowLaunch)
    expect(id).toBe('workflow_run_tool')
    expect(el!.type).toBe(WorkflowRunCard)
    expect(el!.props).toMatchObject({ runId: 'wf_abc123', slot: 'zzq-slot' })
    expect(row).toHaveBeenCalledTimes(1)
    expect(row.mock.calls[0][1]).toBe(true)
  })

  it('draws a sub-agent launch with the parsed ids, wrapped by the host', () => {
    const { el, id, row } = drawn(subagentLaunch)
    expect(id).toBe('subagent_run_tool')
    expect(el!.type).toBe(SubagentRunCard)
    expect(el!.props.slot).toBe('zzq-slot')
    expect(el!.props.launch.ids).toEqual(['1a2b3c4d', '5e6f7a8b'])
    expect(row).toHaveBeenCalledTimes(1)
    expect(row.mock.calls[0][1]).toBe(true)
  })

  // The match guard makes these unreachable through resolution; they exist so a
  // null extraction degrades to the generic line instead of crashing the row.
  it('falls back to the generic tool line when the workflow id will not extract', () => {
    const { ctx } = ctxOf()
    const el = entryById('workflow_run_tool').render(msg('tool', { content: '🔧 workflow_run' }), ctx) as ReactElement
    expect(el.type).toBe(ToolCallLine)
  })

  it('falls back to the generic tool line when the spawn launch will not parse', () => {
    const { ctx } = ctxOf()
    const el = entryById('subagent_run_tool').render(msg('tool', { content: '🔧 spawn_run' }), ctx) as ReactElement
    expect(el.type).toBe(ToolCallLine)
  })
})

describe('the refined role rows', () => {
  it('draws a recovery inject through RecoveryCard with the parsed payload', () => {
    const m = msg('inject', { content: '[Stalled turn — automatic recovery]\nplease continue' })
    const { el, id } = drawn(m)
    expect(id).toBe('recovery_inject')
    expect(el!.type).toBe(RecoveryCard)
    expect(el!.props.parsed).not.toBeNull()
    expect(el!.props.disclosureKey).toBe('zzq-key')
  })

  it('draws nothing for an inject that stops parsing as a recovery', () => {
    const { ctx } = ctxOf()
    expect(entryById('recovery_inject').render(msg('inject', { content: 'zzq ordinary' }), ctx)).toBeNull()
  })

  it('draws a workflow completion through its card, wrapped by the host', () => {
    const m = msg('assistant', {
      content: '[Workflow completion event]\nWorkflow `demo` (wf_abc123) → **finished**\nResult: ok\n',
    })
    const { el, id, row } = drawn(m)
    expect(id).toBe('workflow_completion')
    expect(el!.type).toBe(WorkflowCompletionCard)
    expect(el!.props.onFileOpen).toBe(OPTS.onFileOpen)
    expect(el!.props.onFolderOpen).toBe(OPTS.onFolderOpen)
    expect(row).toHaveBeenCalledTimes(1)
    expect(row.mock.calls[0][1]).toBe(true)
  })
})

describe('the error row', () => {
  it('withholds Continue when the transcript holds no error at all', () => {
    // lastErrorIndex returns -1, so no row can be "the last error".
    const m = msg('error', { content: 'zzq boom' })
    const { el } = drawn(
      m,
      { index: 0, messages: [msg('assistant', { content: 'zzq' })] },
      { slot: 'zzq-slot', continuable: true, interrupted: true, onContinue: () => {} },
    )
    expect(el!.type).toBe(ErrorCard)
    expect(el!.props.content).toBe('zzq boom')
    expect(el!.props.onContinue).toBeUndefined()
  })

  it('offers Continue on the last error and forwards the continuing flag', () => {
    const errs = [msg('error', { content: 'zzq first' }), msg('error', { content: 'zzq last' })]
    const onContinue = () => {}
    const { el } = drawn(
      errs[1],
      { index: 1, messages: errs },
      { slot: 'zzq-slot', continuable: true, interrupted: true, continuing: true, onContinue },
    )
    expect(el!.props.onContinue).toBe(onContinue)
    expect(el!.props.continuing).toBe(true)
  })
})
