import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

function makeTurn(items: TurnItem[], complete = true): Extract<DisplayItem, {kind:'turn'}> {
  return { kind: 'turn', items, complete }
}

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
