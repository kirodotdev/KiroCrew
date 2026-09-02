import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

function makeTurn(items: TurnItem[], complete = true): Extract<DisplayItem, {kind:'turn'}> {
  return { kind: 'turn', items, complete }
}

describe('TurnBlock — interim fan-out fold', () => {
  const renderItem = (it: TurnItem, i: number) => (
    <div data-testid={`item-${i}`} data-role={it.kind === 'single' ? it.msg.role : 'group'}>
      {it.kind === 'single' ? it.msg.content : 'group'}
    </div>
  )
  /** The interim region of a fan-out: a per-completion summary plus an error. */
  const interimItems = (): TurnItem[] => [
    { kind: 'single', msg: { role: 'assistant', content: 'Two of three agents are in…', ts: '1' }, idx: 0 },
    { kind: 'single', msg: { role: 'error', content: 'a spawn failed', ts: '2' }, idx: 1 },
  ]
  /** The element CollapsibleSection wraps its children in. */
  const collapsed = (c: HTMLElement) => c.querySelector('[style*="overflow: hidden"]')

  it('folds the region behind one toggle in DEFAULT mode, where nothing folded before', () => {
    const turn = { ...makeTurn(interimItems()), interim: true }
    const { container } = render(<TurnBlock turn={turn} renderItem={renderItem} />)
    // The interim summary is inside the collapsible; the toggle is its control.
    expect(container.querySelector('button')).toBeInTheDocument()
    expect(collapsed(container)).toContainElement(screen.getByTestId('item-0'))
  })

  it('leaves an error row outside the fold', () => {
    const turn = { ...makeTurn(interimItems()), interim: true }
    const { container } = render(<TurnBlock turn={turn} renderItem={renderItem} />)
    expect(collapsed(container)).not.toContainElement(screen.getByTestId('item-1'))
  })

  it('does not fold while the turn is still running', () => {
    const turn = { ...makeTurn(interimItems(), false), interim: true }
    const { container } = render(<TurnBlock turn={turn} renderItem={renderItem} />)
    expect(container.querySelector('button')).toBeNull()
    expect(collapsed(container)).toBeNull()
  })

  it('an identical turn without the interim flag is untouched in default mode', () => {
    const turn = makeTurn(interimItems())
    const { container } = render(<TurnBlock turn={turn} renderItem={renderItem} />)
    expect(container.querySelector('button')).toBeNull()
    expect(screen.getByTestId('item-0')).toBeInTheDocument()
  })
})

describe('TurnBlock — file role visibility', () => {
  it('file messages are not collapsed behind reasoning toggle', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: file_send', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'file', content: '{"filename":"test.mp3","content_type":"audio/mpeg"}', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'assistant', content: 'Here is your file.', ts: '3' }, idx: 2 },
    ]
    const turn = makeTurn(items)
    render(
      <TurnBlock
        turn={turn}
        renderItem={(it) => <div data-testid={`item-${it.kind === 'single' ? it.msg.role : 'group'}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
      />
    )
    // File message should be visible (not hidden behind collapse)
    expect(screen.getByTestId('item-file')).toBeInTheDocument()
    // Assistant message should also be visible
    expect(screen.getByTestId('item-assistant')).toBeInTheDocument()
  })

  it('file messages visible even in collapseAll mode', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: edge-tts', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'file', content: '{"filename":"standup.mp3","content_type":"audio/mpeg"}', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'assistant', content: 'Generated your standup audio.', ts: '3' }, idx: 2 },
    ]
    const turn = makeTurn(items)
    render(
      <TurnBlock
        turn={turn}
        renderItem={(it) => <div data-testid={`item-${it.kind === 'single' ? it.msg.role : 'group'}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
        collapseAll={true}
      />
    )
    // In collapseAll mode, file is a "conclusion" so it should be visible
    expect(screen.getByTestId('item-file')).toBeInTheDocument()
  })

  it('file message mid-turn stays visible in collapseAll mode (not folded into reasoning)', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: file_send', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'file', content: '{"filename":"clip.wav","content_type":"audio/x-wav"}', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: shell', ts: '3' }, idx: 2 },
      { kind: 'single', msg: { role: 'assistant', content: 'Sent the audio clip. Can you see the player?', ts: '4' }, idx: 3 },
    ]
    const turn = makeTurn(items)
    const { container } = render(
      <TurnBlock
        turn={turn}
        renderItem={(it, i) => <div data-testid={`item-${i}`} data-role={it.kind === 'single' ? it.msg.role : 'group'}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
        collapseAll={true}
      />
    )
    // File message at idx 1 must be visible (not inside collapsed overflow:hidden section)
    const fileItem = container.querySelector('[data-testid="item-1"]')
    expect(fileItem).not.toBeNull()
    expect(fileItem?.closest('[style*="overflow"]')).toBeNull()
    // Conclusion still visible
    expect(container.querySelector('[data-testid="item-3"]')).not.toBeNull()
  })

  it('keep-visible marked report mid-turn stays visible in collapseAll mode (#7948)', () => {
    const report =
      'Fleet synthesis: 44/44 runs banked, all routing gates PASS, medians in the artifact. '.repeat(3) +
      '\n\n<!-- keep-visible -->'
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'assistant', content: report, ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: autonudge_stop', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'assistant', content: 'Loop stopped. Campaign closed out; summaries and restores all verified done.', ts: '3' }, idx: 2 },
    ]
    const turn = makeTurn(items)
    const { container } = render(
      <TurnBlock
        turn={turn}
        renderItem={(it, i) => <div data-testid={`item-${i}`} data-role={it.kind === 'single' ? it.msg.role : 'group'}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
        collapseAll={true}
      />
    )
    // The marked report (idx 0) must render in place, not inside a collapsed
    // (overflow-hidden) section — same assertion shape as the file-row test.
    const reportItem = container.querySelector('[data-testid="item-0"]')
    expect(reportItem).not.toBeNull()
    expect(reportItem?.closest('[style*="overflow"]')).toBeNull()
    // Conclusion still visible
    expect(container.querySelector('[data-testid="item-2"]')).not.toBeNull()
  })

  it('unmarked mid-turn report folds into the collapse pane (control for #7948)', () => {
    const report =
      'Fleet synthesis: 44/44 runs banked, all routing gates PASS, medians in the artifact. '.repeat(3)
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'assistant', content: report, ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: autonudge_stop', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'assistant', content: 'Loop stopped. Campaign closed out; summaries and restores all verified done.', ts: '3' }, idx: 2 },
    ]
    const turn = makeTurn(items)
    const { container } = render(
      <TurnBlock
        turn={turn}
        renderItem={(it, i) => <div data-testid={`item-${i}`} data-role={it.kind === 'single' ? it.msg.role : 'group'}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
        collapseAll={true}
      />
    )
    // Without the marker the report is intermediate reasoning: it either does
    // not render or sits inside a collapsed overflow section. This pins the
    // user-preference contract the marker deliberately opts OUT of.
    const reportItem = container.querySelector('[data-testid="item-0"]')
    expect(reportItem === null || reportItem.closest('[style*="overflow"]') !== null).toBe(true)
    // The conclusion is the visible survivor.
    const conclusion = container.querySelector('[data-testid="item-2"]')
    expect(conclusion).not.toBeNull()
    expect(conclusion?.closest('[style*="overflow"]')).toBeNull()
  })

  it('renders file in its original turn position (not hoisted to top)', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'assistant', content: 'generating audio…', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'file', content: '{"filename":"a.mp3","content_type":"audio/mpeg"}', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'assistant', content: 'here it is', ts: '3' }, idx: 2 },
    ]
    const turn = makeTurn(items)
    const { container } = render(
      <TurnBlock
        turn={turn}
        renderItem={(it) => <div data-testid={`item-${it.kind === 'single' ? it.msg.role + '-' + it.idx : 'group'}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
      />
    )
    const rendered = Array.from(container.querySelectorAll('[data-testid^="item-"]'))
    const order = rendered.map(el => el.getAttribute('data-testid'))
    expect(order).toEqual(['item-assistant-0', 'item-file-1', 'item-assistant-2'])
  })
})

describe('TurnBlock — renderable content stays visible in collapseAll mode', () => {
  it('mcwidget emitted between tool calls is not folded into the reasoning pane', () => {
    const widgetBody = '<mcwidget title="Hello">\n<div>hi</div>\n</mcwidget>'
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'assistant', content: widgetBody, ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: artifact_save', ts: '3' }, idx: 2 },
      { kind: 'single', msg: { role: 'assistant', content: 'Saved as artifact `hello-world` (v1). Let me know if you want changes.', ts: '4' }, idx: 3 },
    ]
    const turn = makeTurn(items)
    const { container } = render(
      <TurnBlock
        turn={turn}
        renderItem={(it, i) => (
          <div data-testid={`item-${i}`} data-role={it.kind === 'single' ? it.msg.role : 'group'}>
            {it.kind === 'single' ? it.msg.content : 'group'}
          </div>
        )}
        collapseAll={true}
      />
    )
    // The widget-bearing assistant message must render outside the collapsed reasoning section.
    const widgetItem = container.querySelector('[data-testid="item-1"]')
    expect(widgetItem).not.toBeNull()
    // It should NOT be a descendant of a CollapsibleSection (motion.div with overflow:hidden).
    const collapsedAncestors = widgetItem?.closest('[style*="overflow"]') ?? null
    expect(collapsedAncestors).toBeNull()
    // The conclusion (last assistant message) is still visible.
    expect(container.querySelector('[data-testid="item-3"]')).not.toBeNull()
  })

  it('image embed in mid-turn assistant text stays visible in collapseAll mode', () => {
    const imgMsg = 'See the chart: ![chart](/tmp/chart.png)'
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: shell', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'assistant', content: imgMsg, ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: shell', ts: '3' }, idx: 2 },
      { kind: 'single', msg: { role: 'assistant', content: 'Done — uploaded to S3 and verified the link works.', ts: '4' }, idx: 3 },
    ]
    const turn = makeTurn(items)
    const { container } = render(
      <TurnBlock
        turn={turn}
        renderItem={(it, i) => <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
        collapseAll={true}
      />
    )
    const imgItem = container.querySelector('[data-testid="item-1"]')
    expect(imgItem).not.toBeNull()
    expect(imgItem?.closest('[style*="overflow"]')).toBeNull()
  })

  it('plain prose between tool calls still collapses (no regression)', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'assistant', content: 'Inspecting the config file before patching.', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: write', ts: '3' }, idx: 2 },
      { kind: 'single', msg: { role: 'assistant', content: 'Patched the config and verified the build still passes.', ts: '4' }, idx: 3 },
    ]
    const turn = makeTurn(items)
    const { container } = render(
      <TurnBlock
        turn={turn}
        renderItem={(it, i) => <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
        collapseAll={true}
      />
    )
    // Plain prose at idx 1 should be inside a collapsed (overflow:hidden) section.
    const proseItem = container.querySelector('[data-testid="item-1"]')
    expect(proseItem).not.toBeNull()
    expect(proseItem?.closest('[style*="overflow"]')).not.toBeNull()
    // Conclusion still visible.
    expect(container.querySelector('[data-testid="item-3"]')).not.toBeNull()
  })
})

/**
 * A spawn_run launch renders as SubagentRunCard, so like a workflow_run launch
 * it must bypass the collapsible tool group. Folding it in is what left a
 * spawned wave with no record in scrollback beyond "Worked through N steps".
 */
describe('TurnBlock — spawn_run launch visibility', () => {
  const SPAWN_OUTPUT = [
    'Spawned 2 subagent(s). Results will arrive as completion events:',
    '  1713e7d0 (kirocrew): read the specs',
    '  5c15adde (kirocrew): read the code',
  ].join('\n')

  const spawnItem = (idx: number): TurnItem => ({
    kind: 'single',
    msg: { role: 'tool', content: '🔧 spawn_run', ts: `${idx}`, meta: { output: SPAWN_OUTPUT } },
    idx,
  })

  it('is rendered inline, not folded into the collapsed tool group', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: fs_read', ts: '1' }, idx: 0 },
      spawnItem(1),
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: fs_read', ts: '3' }, idx: 2 },
      { kind: 'single', msg: { role: 'assistant', content: 'Spawned 2 agents.', ts: '4' }, idx: 3 },
    ]
    render(
      <TurnBlock
        turn={makeTurn(items)}
        renderItem={(it) => (
          <div data-testid={it.kind === 'single' && it.msg.content === '🔧 spawn_run' ? 'item-spawn' : `item-${it.kind === 'single' ? it.msg.role : 'group'}`}>
            {it.kind === 'single' ? it.msg.content : 'group'}
          </div>
        )}
      />,
    )
    expect(screen.getByTestId('item-spawn')).toBeInTheDocument()
  })

})

/**
 * An MCP App (SEP-1865) render mounts an interactive iframe anchored to its
 * tool-call row. Folding that row into a collapsible pane hides the app, and
 * re-expanding REMOUNTS the iframe — reloading it and losing in-canvas state.
 * So an app-bearing row must bypass the collapse in both modes. The set is a
 * prop (not Redux) because TurnBlock also renders under app-sdk/ChatEmbed,
 * which mounts no Provider.
 */
describe('TurnBlock — MCP App-bearing tool calls stay visible', () => {
  const items: TurnItem[] = [
    { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read_me', ts: '1', meta: { tool_call_id: 'tc-plain' } }, idx: 0 },
    { kind: 'single', msg: { role: 'tool', content: '🔧 Running: create_view', ts: '2', meta: { tool_call_id: 'tc-app-1' } }, idx: 1 },
    { kind: 'single', msg: { role: 'assistant', content: 'Rendered a diagram with plenty of descriptive text to be substantive.', ts: '3' }, idx: 2 },
  ]

  const renderApp = (collapseAll: boolean, appIds: ReadonlySet<string>) =>
    render(
      <TurnBlock
        turn={makeTurn(items)}
        collapseAll={collapseAll}
        appToolCallIds={appIds}
        renderItem={(it) => (
          <div data-testid={`item-${it.kind === 'single' ? `${it.msg.role}-${(it.msg.meta?.tool_call_id as string) ?? 'x'}` : 'group'}`} />
        )}
      />,
    )

  it('default mode: app-bearing row renders outside the collapsed tool group', () => {
    renderApp(false, new Set(['tc-app-1']))
    // The app-bearing row is visible without expanding anything…
    expect(screen.getByTestId('item-tool-tc-app-1')).toBeInTheDocument()
    // …while the plain tool call stays behind the collapse (unmounted).
    expect(screen.queryByTestId('item-tool-tc-plain')).not.toBeInTheDocument()
  })

  it('collapseAll mode: app-bearing row renders outside the reasoning pane', () => {
    renderApp(true, new Set(['tc-app-1']))
    expect(screen.getByTestId('item-tool-tc-app-1')).toBeInTheDocument()
    expect(screen.getByTestId('item-assistant-x')).toBeInTheDocument()
  })

  it('without the prop, tool rows collapse exactly as before (embed/no-store path)', () => {
    renderApp(false, new Set())
    expect(screen.queryByTestId('item-tool-tc-app-1')).not.toBeInTheDocument()
    expect(screen.queryByTestId('item-tool-tc-plain')).not.toBeInTheDocument()
  })
})

/**
 * An edit-tool row whose persisted meta carries a unified diff promotes an
 * inline diff presentation (ToolCallLine renders a card or summary chip), so
 * like a workflow_run launch it stays out of BOTH folds — it is the primary
 * display of the file change now that the model no longer restates tool
 * edits as ```diff blocks. Density relief is per-card (ToolCallLine's
 * fold control in the card header), not a mode of this component.
 */
describe('TurnBlock — diff-card tool rows', () => {
  const DIFF = '--- /a/b.py\n+++ /a/b.py\n@@ -1,2 +1,2 @@\n import os\n-x = 1\n+x = 2'
  const items: TurnItem[] = [
    { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read_me', ts: '1', meta: { tool_call_id: 'tc-plain' } }, idx: 0 },
    { kind: 'single', msg: { role: 'tool', content: '🔧 fs_write', ts: '2', meta: { tool_call_id: 'tc-edit', kind: 'edit', input: DIFF } }, idx: 1 },
    { kind: 'single', msg: { role: 'assistant', content: 'Edited the file with plenty of descriptive text to be substantive here.', ts: '3' }, idx: 2 },
  ]

  const renderDiffTurn = (collapseAll: boolean) =>
    render(
      <TurnBlock
        turn={makeTurn(items)}
        collapseAll={collapseAll}
        renderItem={(it) => (
          <div data-testid={`item-${it.kind === 'single' ? `${it.msg.role}-${(it.msg.meta?.tool_call_id as string) ?? 'x'}` : 'group'}`} />
        )}
      />,
    )

  it('default mode: the diff-card row renders outside the collapsed tool group', () => {
    renderDiffTurn(false)
    expect(screen.getByTestId('item-tool-tc-edit')).toBeInTheDocument()
    expect(screen.queryByTestId('item-tool-tc-plain')).not.toBeInTheDocument()
  })

  it('collapseAll mode: the diff row stays visible-inline', () => {
    renderDiffTurn(true)
    // The plain read folds ("Worked through 1 step"), the edit row does not.
    expect(screen.getByText('Worked through 1 step')).toBeInTheDocument()
    expect(screen.getByTestId('item-tool-tc-edit')).toBeInTheDocument()
    expect(screen.getByTestId('item-assistant-x')).toBeInTheDocument()
  })

  it('an execute-kind row with diff-shaped input still collapses (kind gate)', () => {
    const shellItems: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 shell', ts: '1', meta: { tool_call_id: 'tc-sh', kind: 'execute', input: DIFF } }, idx: 0 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 shell 2', ts: '2', meta: { tool_call_id: 'tc-sh2', kind: 'execute', input: 'ls' } }, idx: 1 },
      { kind: 'single', msg: { role: 'assistant', content: 'Ran the commands with plenty of descriptive output text here.', ts: '3' }, idx: 2 },
    ]
    render(
      <TurnBlock
        turn={makeTurn(shellItems)}
        renderItem={(it) => (
          <div data-testid={`item-${it.kind === 'single' ? `${it.msg.role}-${(it.msg.meta?.tool_call_id as string) ?? 'x'}` : 'group'}`} />
        )}
      />,
    )
    expect(screen.queryByTestId('item-tool-tc-sh')).not.toBeInTheDocument()
  })
})

/**
 * A turn can hand back to the user and then RESUME in the same turn — after a
 * denied tool call, an auto-nudge / monitor cycle, a queued message, or an
 * injected subagent / workflow completion. The [OPTIONS:] follow-up marker is
 * the agent's own signal that it believed it was ending the turn, so an earlier
 * hand-back carrying it must stay visible rather than collapse behind "Worked
 * through N steps" (findConclusionIdx keeps only the LAST conclusion).
 */
describe('TurnBlock — mid-turn hand-back ([OPTIONS:]) visibility', () => {
  it('a mid-turn hand-back carrying [OPTIONS:] stays visible in collapseAll mode', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: shell', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'assistant', content: 'Here is the full setup runbook you asked for, with every step spelled out.\n\n[OPTIONS: Run it now | Show me the diff]', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: shell', ts: '3' }, idx: 2 },
      { kind: 'single', msg: { role: 'assistant', content: 'Resumed after the hand-back and finished wiring everything up and verifying it.', ts: '4' }, idx: 3 },
    ]
    const { container } = render(
      <TurnBlock
        turn={makeTurn(items)}
        renderItem={(it, i) => <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
        collapseAll={true}
      />
    )
    // The earlier hand-back at idx 1 must render OUTSIDE the collapsed
    // (overflow:hidden) reasoning section — it is a real deliverable.
    const handBack = container.querySelector('[data-testid="item-1"]')
    expect(handBack).not.toBeNull()
    expect(handBack?.closest('[style*="overflow"]')).toBeNull()
    // The final conclusion is still visible too.
    expect(container.querySelector('[data-testid="item-3"]')).not.toBeNull()
  })

  it('a mid-turn assistant message WITHOUT an options marker still collapses (predicate is not over-broad)', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'assistant', content: 'Reading the config file before I patch it, to be sure of its shape.', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: write', ts: '3' }, idx: 2 },
      { kind: 'single', msg: { role: 'assistant', content: 'Patched the config and confirmed the build still passes cleanly.', ts: '4' }, idx: 3 },
    ]
    const { container } = render(
      <TurnBlock
        turn={makeTurn(items)}
        renderItem={(it, i) => <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
        collapseAll={true}
      />
    )
    // Plain reasoning at idx 1 (no [OPTIONS:] marker) must stay INSIDE the
    // collapsed section — surfacing it would defeat "hide intermediate reasoning".
    const prose = container.querySelector('[data-testid="item-1"]')
    expect(prose).not.toBeNull()
    expect(prose?.closest('[style*="overflow"]')).not.toBeNull()
    // Conclusion still visible.
    expect(container.querySelector('[data-testid="item-3"]')).not.toBeNull()
  })

  it('keeps crew-mode answers out of the collapse pane', () => {
    // Crew Mode inverts this component's core assumption: every forwarded
    // completion is the FINAL answer for a different topic, so "last assistant
    // message is the conclusion" would bury real answers behind the toggle.
    // Marked via the persisted `crew-reply` class so it survives a reload.
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'assistant', content: 'Got it — working on that.', cls: 'msg msg-a', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'assistant', content: "Here's what's in flight: three topics running right now.", cls: 'msg msg-a crew-reply', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'assistant', content: '↩ re: "check why the stable feed returns 403"\n\nRoot cause: the origin rejects the stale signing key.', cls: 'msg msg-a crew-reply', ts: '3' }, idx: 2 },
      { kind: 'single', msg: { role: 'assistant', content: '↩ re: "explain the TTL sweep"\n\nIt runs every 6h and compacts afterwards.', cls: 'msg msg-a crew-reply', ts: '4' }, idx: 3 },
    ]
    const { container } = render(
      <TurnBlock
        turn={makeTurn(items)}
        renderItem={(it, i) => <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
        collapseAll={true}
      />
    )
    // All three answers render OUTSIDE the collapsible pane...
    for (const i of [1, 2, 3]) {
      const el = container.querySelector(`[data-testid="item-${i}"]`)
      expect(el).not.toBeNull()
      expect(el?.closest('[style*="overflow"]')).toBeNull()
    }
    // ...while the templated ack is still free to fold away.
    expect(container.querySelector('[data-testid="item-0"]')?.closest('[style*="overflow"]')).not.toBeNull()
  })

  it('does not treat a stray class containing "crew-reply" as a marker', () => {
    // Substring safety: the match is on a whole class token, so a class like
    // "not-crew-reply-thing" must not smuggle a message past the collapse.
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'assistant', content: 'intermediate reasoning that should stay hidden', cls: 'msg msg-a not-crew-replyish', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'assistant', content: 'the actual conclusion of this turn, long enough to count.', cls: 'msg msg-a', ts: '2' }, idx: 1 },
    ]
    const { container } = render(
      <TurnBlock
        turn={makeTurn(items)}
        renderItem={(it, i) => <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
        collapseAll={true}
      />
    )
    expect(container.querySelector('[data-testid="item-0"]')?.closest('[style*="overflow"]')).not.toBeNull()
  })

  it('the "Worked through N steps" count excludes the now-visible hand-back', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: shell', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'assistant', content: 'First deliverable — the runbook is ready for your review below.\n\n[OPTIONS: Run it now | Wait]', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: shell', ts: '3' }, idx: 2 },
      { kind: 'single', msg: { role: 'assistant', content: 'Resumed and completed the remaining work, everything verified and green.', ts: '4' }, idx: 3 },
    ]
    render(
      <TurnBlock
        turn={makeTurn(items)}
        renderItem={(it, i) => <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>}
        collapseAll={true}
      />
    )
    // Two tool calls collapse (2 steps); the hand-back at idx 1 is surfaced
    // inline and must NOT inflate the count to 3.
    expect(screen.getByRole('button', { name: /Worked through 2 steps/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Worked through 3 steps/ })).not.toBeInTheDocument()
  })
})

/**
 * chatSlice opens one `thinking` message per reasoning burst (one above each
 * tool step it explains, #4178). A long agentic turn therefore settles into a
 * WALL of collapsed "Thought process" rows once the interleaved tool calls
 * fold away. TurnBlock now folds every content-bearing burst of a turn into a
 * SINGLE reasoning row, anchored at the first burst's position, so the turn
 * carries one block (render-only; the store keeps the per-burst messages).
 */
describe('TurnBlock — reasoning bursts fold into one thinking row', () => {
  const think = (idx: number, content: string, clientTs: string): TurnItem => ({
    kind: 'single',
    msg: { role: 'thinking', content, cls: '', meta: { clientTs }, ts: `${idx}` },
    idx,
  })

  const revealThinking = (it: TurnItem, i: number) => (
    <div
      data-testid={`item-${i}`}
      data-role={it.kind === 'single' ? it.msg.role : 'group'}
    >
      {it.kind === 'single' ? it.msg.content : 'group'}
    </div>
  )

  it('merges every burst of a turn into one row with concatenated content', () => {
    const items: TurnItem[] = [
      think(0, 'first burst', 'a'),
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read', ts: '1' }, idx: 1 },
      think(2, 'second burst', 'b'),
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: shell', ts: '3' }, idx: 3 },
      think(4, 'third burst', 'c'),
      { kind: 'single', msg: { role: 'assistant', content: 'Final answer.', ts: '5' }, idx: 5 },
    ]
    const { container } = render(<TurnBlock turn={makeTurn(items)} renderItem={revealThinking} />)
    // Exactly ONE thinking row, not three.
    const thinkingRows = container.querySelectorAll('[data-role="thinking"]')
    expect(thinkingRows.length).toBe(1)
    // …carrying every burst's text in order.
    const text = thinkingRows[0].textContent || ''
    expect(text).toContain('first burst')
    expect(text).toContain('second burst')
    expect(text).toContain('third burst')
    // The conclusion is still visible.
    expect(screen.getByText('Final answer.')).toBeInTheDocument()
  })

  it('a single-burst turn is left untouched (no synthetic row)', () => {
    const items: TurnItem[] = [
      think(0, 'only burst', 'a'),
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read', ts: '1' }, idx: 1 },
      { kind: 'single', msg: { role: 'assistant', content: 'Answer.', ts: '2' }, idx: 2 },
    ]
    const { container } = render(<TurnBlock turn={makeTurn(items)} renderItem={revealThinking} />)
    const thinkingRows = container.querySelectorAll('[data-role="thinking"]')
    expect(thinkingRows.length).toBe(1)
    expect(thinkingRows[0].textContent).toBe('only burst')
  })

  it('folds while the turn is still running (live per-burst wall is prevented)', () => {
    // An incomplete turn takes TurnBlock's inline early-return path; the merge
    // must apply there too so a running turn shows ONE growing reasoning line.
    const items: TurnItem[] = [
      think(0, 'burst one', 'a'),
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read', ts: '1' }, idx: 1 },
      think(2, 'burst two', 'b'),
    ]
    const { container } = render(<TurnBlock turn={makeTurn(items, false)} renderItem={revealThinking} />)
    const thinkingRows = container.querySelectorAll('[data-role="thinking"]')
    expect(thinkingRows.length).toBe(1)
    expect(thinkingRows[0].textContent).toContain('burst one')
    expect(thinkingRows[0].textContent).toContain('burst two')
  })

  it('skips empty placeholder thinking rows when choosing the merge anchor', () => {
    // An empty "Thinking…" placeholder must not become the merged row (it would
    // render nothing); the first CONTENT-bearing burst anchors the block.
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'thinking', content: '', cls: '', meta: { clientTs: 'p' }, ts: '0' }, idx: 0 },
      think(1, 'real burst one', 'a'),
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read', ts: '2' }, idx: 2 },
      think(3, 'real burst two', 'b'),
    ]
    const { container } = render(<TurnBlock turn={makeTurn(items, false)} renderItem={revealThinking} />)
    const withText = Array.from(container.querySelectorAll('[data-role="thinking"]'))
      .filter(el => (el.textContent || '').trim().length > 0)
    expect(withText.length).toBe(1)
    expect(withText[0].textContent).toContain('real burst one')
    expect(withText[0].textContent).toContain('real burst two')
  })

  it('hoists the folded reasoning to the turn TOP when bursts arrive at the tail (post-chat_done refresh)', () => {
    // On the chat_done refresh, mergePreservedThinking can park bursts that lack
    // a distinct following-tool anchor at the tail, below the answer (#4218
    // residual). The fold must render the merged reasoning ABOVE the answer
    // regardless of where the refresh left the bursts.
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: shell', ts: '2' }, idx: 1 },
      { kind: 'single', msg: { role: 'assistant', content: 'Conclusion first, reasoning was piled below it.', ts: '3' }, idx: 2 },
      think(3, 'tail burst one', 'a'),
      think(4, 'tail burst two', 'b'),
      think(5, 'tail burst three', 'c'),
    ]
    const { container } = render(<TurnBlock turn={makeTurn(items)} renderItem={revealThinking} />)
    // One merged reasoning row carrying every burst…
    const thinkingRows = container.querySelectorAll('[data-role="thinking"]')
    expect(thinkingRows.length).toBe(1)
    const text = thinkingRows[0].textContent || ''
    expect(text).toContain('tail burst one')
    expect(text).toContain('tail burst two')
    expect(text).toContain('tail burst three')
    // …rendered ABOVE the answer, not below it.
    const roled = Array.from(container.querySelectorAll('[data-role="thinking"], [data-role="assistant"]'))
    expect(roled[0].getAttribute('data-role')).toBe('thinking')
    expect(roled[roled.length - 1].getAttribute('data-role')).toBe('assistant')
  })

  it('hoists a SINGLE tail burst to the top too (single-burst reload)', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read', ts: '1' }, idx: 0 },
      { kind: 'single', msg: { role: 'assistant', content: 'The answer, with the lone reasoning burst stranded below it.', ts: '2' }, idx: 1 },
      think(2, 'stranded burst', 'a'),
    ]
    const { container } = render(<TurnBlock turn={makeTurn(items)} renderItem={revealThinking} />)
    const thinkingRows = container.querySelectorAll('[data-role="thinking"]')
    expect(thinkingRows.length).toBe(1)
    expect(thinkingRows[0].textContent).toBe('stranded burst')
    const roled = Array.from(container.querySelectorAll('[data-role="thinking"], [data-role="assistant"]'))
    expect(roled[0].getAttribute('data-role')).toBe('thinking')
  })
})
