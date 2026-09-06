import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

// The four chat-surface action rows hide behind `opacity-0` +
// `group-hover/*:opacity-100`, which a touch pointer can never trigger —
// without the `(hover: none)` escape hatch the actions are permanently
// invisible on phones (issue #3584, same defect class as #2014). happy-dom
// does not evaluate media queries, so these tests pin the utility classes
// themselves, exactly the way src/test/AssistantMessage.test.tsx pins the
// assistant footer's.

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn().mockResolvedValue({
      svg:
        '<svg ' +
        'viewBox="0 0 240 120" aria-roledescription="flowchart-v2"><g class="nodes"></g></svg>',
    }),
  },
}))

import { CodeBlock } from '../components/CodeBlock'
import DiffBlock from '../components/DiffBlock'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { PinnedMessagesPanel } from '../pages/chat/PinnedMessagesPanel'
import type { ChatPin } from '../api/pins'

const ROW_TOUCH_CLASSES = [
  '[@media(hover:none)]:opacity-100',
  '[@media(hover:none)]:flex-wrap',
  '[@media(hover:none)]:[&_button]:p-3',
  '[@media(hover:none)]:[&_svg]:h-4',
  '[@media(hover:none)]:[&_svg]:w-4',
]

const expectRowTouchOverrides = (cls: string) => {
  for (const c of ROW_TOUCH_CLASSES) expect(cls).toContain(c)
}

const SIMPLE_DIFF = `--- a/file.ts
+++ b/file.ts
@@ -1,2 +1,2 @@
 const a = 1
-const b = 2
+const b = 3`

const MOCK_PIN: ChatPin = {
  id: 'pin-1',
  slot_key: 'slot-abc',
  mid: 'm-touch-test-1',
  message_ts: '2026-08-01T10:00:00Z',
  role: 'assistant',
  preview: 'Pinned answer preview',
  pinned_at: '2026-08-01T12:00:00Z',
}

const renderPins = () =>
  render(
    <PinnedMessagesPanel
      pins={[MOCK_PIN]}
      loading={false}
      slotKey="slot-abc"
      onClose={() => {}}
      onJumpToMessage={() => {}}
      onUnpin={() => {}}
    />,
  )

describe('PinnedMessagesPanel row actions on touch devices', () => {
  const row = (container: HTMLElement) =>
    container.querySelector('[class*="group-hover/pin"]') as HTMLElement

  it('reveals and enlarges the pin actions where the pointer cannot hover', () => {
    const { container } = renderPins()
    expectRowTouchOverrides(row(container).className)
  })

  it('keeps the pin actions hover-revealed for hover-capable pointers', () => {
    const { container } = renderPins()
    const cls = row(container).className
    expect(cls).toContain('opacity-0')
    expect(cls).toContain('group-hover/pin:opacity-100')
  })
})

describe('CodeBlock header actions on touch devices', () => {
  const row = (container: HTMLElement) =>
    container.querySelector('[class*="group-hover/code"]') as HTMLElement

  it('reveals and enlarges the copy action where the pointer cannot hover', () => {
    const { container } = render(<CodeBlock code="const x = 1" lang="ts" complete />)
    expectRowTouchOverrides(row(container).className)
  })

  it('keeps the copy action hover-revealed for hover-capable pointers', () => {
    const { container } = render(<CodeBlock code="const x = 1" lang="ts" complete />)
    const cls = row(container).className
    expect(cls).toContain('opacity-0')
    expect(cls).toContain('group-hover/code:opacity-100')
    expect(cls).toContain('group-focus-within/code:opacity-100')
  })
})

describe('DiffBlock header actions on touch devices', () => {
  const row = (container: HTMLElement) =>
    container.querySelector('[class*="group-hover/diff"]') as HTMLElement

  // Pierre slots the header metadata after mount, so the actions row is absent
  // from the first commit -- await it instead of reading a null node.
  it('reveals and enlarges the diff actions where the pointer cannot hover', async () => {
    const { container } = render(<DiffBlock code={SIMPLE_DIFF} complete />)
    await screen.findByTitle('Copy patch')
    expectRowTouchOverrides(row(container).className)
  })

  it('keeps the diff actions hover-revealed for hover-capable pointers', async () => {
    const { container } = render(<DiffBlock code={SIMPLE_DIFF} complete />)
    await screen.findByTitle('Copy patch')
    const cls = row(container).className
    expect(cls).toContain('opacity-0')
    expect(cls).toContain('group-hover/diff:opacity-100')
    expect(cls).toContain('group-focus-within/diff:opacity-100')
  })
})

describe('MarkdownRenderer diagram action row on touch devices', () => {
  // This surface WAS a single absolutely-positioned button and is now a ROW of
  // them, so the touch escape hatch moved from the single-button shape to the
  // row shape. `touchActions.ts` documents that split: only the row form grows a
  // CLUSTER's targets (`[&_button]:p-3`) and lets it wrap, and the single-button
  // form cannot match descendants at all. The contract is unchanged -- actions
  // visible without hover, targets at the 40px floor -- and is now enforced one
  // element further out.
  const renderRow = async () => {
    render(<MarkdownRenderer content={'```mermaid\ngraph TD;A-->B\n```'} />)
    const btn = await waitFor(() =>
      screen.getByRole('button', { name: /enlarge diagram/i }),
    )
    return btn.parentElement as HTMLElement
  }

  it('reveals the cluster and grows its targets where the pointer cannot hover', async () => {
    const cls = (await renderRow()).className
    expect(cls).toContain('[@media(hover:none)]:opacity-100')
    // Padding applies to the row's DESCENDANT buttons — the direct form would
    // land on the row itself and leave every target below the 40px floor.
    expect(cls).toContain('[@media(hover:none)]:[&_button]:p-3')
    expect(cls).not.toContain('[@media(hover:none)]:p-2.5')
    expect(cls).toContain('[@media(hover:none)]:[&_svg]:h-4')
    expect(cls).toContain('[@media(hover:none)]:[&_svg]:w-4')
    // A grown row can exceed a phone's width, so it must wrap rather than clip.
    expect(cls).toContain('[@media(hover:none)]:flex-wrap')
  })

  it('keeps the cluster hover-revealed for hover-capable pointers', async () => {
    const cls = (await renderRow()).className
    expect(cls).toContain('opacity-0')
    expect(cls).toContain('group-hover:opacity-100')
    expect(cls).toContain('group-focus-within:opacity-100')
  })
})
