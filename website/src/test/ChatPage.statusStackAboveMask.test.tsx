import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * The yellow "N sub-agent results ready" bar rendered with its top border, both
 * top corners and the first line's ascenders shaved off — it read as the card
 * being clipped by the UI.
 *
 * Nothing clipped it. The transcript's bottom mask is `relative z-[1]` and
 * deliberately overshoots COMPOSER_MASK_OVERSHOOT_PX BELOW the scrollport edge so
 * it ends flush against the composer box. That overshoot is sized for the distance
 * ChatInput owns (`pt-1` + the `h-[6px]` spacer) — a distance that only holds while
 * the composer status stack is EMPTY. With a bar present the bar occupies that
 * strip, and the mask's tail (opaque: `from-bg from-[62%]` makes the bottom 62%
 * solid, and the overshoot is entirely inside it) painted OVER the bar's top 10px.
 * Measured in an isolated repro of the same column: mask.bottom − bar.top == 10,
 * exactly the overshoot, and lifting the bar changed pixels while leaving every
 * rect identical — paint order, not layout.
 *
 * SubagentProgressBar was immune only by accident: its wave chip already sits at
 * `z-[46]` for an unrelated reason (clearing theme-experience overlays), which is
 * why the defect only surfaced on the bars that had no z-index at all.
 *
 * The fix is per-child rather than one z-index on the stack wrapper on purpose: a
 * z-index there would make the wrapper a stacking context and CONFINE the wave
 * chip's 46, re-exposing it to the overlays it was lifted to clear. So this file
 * pins three things — the ordering, the wrapper staying context-free, and the
 * child LIST, so a sixth bar cannot quietly inherit the bug.
 *
 * Asserted against SOURCE TEXT, as the two neighbouring mask guards already are:
 * the numbers live in five files, several as Tailwind classes jsdom cannot resolve
 * into a paint order, and the invariant is the comparison BETWEEN them.
 */
const SRC = (p: string) => readFileSync(resolve(__dirname, '..', p), 'utf8')
const CHAT_PAGE = SRC('pages/chat/ChatPageView.tsx')

/** The mask every child of the stack has to outrank. */
const MASK_Z = /className="bg-gradient-to-t from-bg from-\[\d+%\] to-transparent pointer-events-none relative z-\[(\d+)\]"/.exec(CHAT_PAGE)
/** The composer's own layer. A bar at or above it would paint over the input box,
 *  and QueueStack's -OVERLAP fuse would surface ON TOP of the composer. */
const COMPOSER_Z = /<div ref=\{inputAreaRef\} className="relative z-(\d+)">/.exec(CHAT_PAGE)
/** The stack wrapper's own className, and the JSX block it encloses. */
const STACK = /<div ref=\{composerBandRef\} className="([^"]*)" data-testid="composer-status-stack">([\s\S]*?)\n {14}<\/div>/.exec(CHAT_PAGE)

/** Every component the stack renders, paired with the z-index its own outermost
 *  wrapper declares. Each value is read out of that component's real source. */
const CHILDREN: Record<string, RegExp> = {
  TaskProgressBar: /<div className="px-4 mx-auto w-full relative z-\[(\d+)\]" style=\{\{ maxWidth: 'var\(--mc-content-width, 900px\)' \}\}>/,
  SubagentProgressBar: /<div className="px-4 mx-auto w-full relative z-\[(\d+)\]" style=\{\{ maxWidth: 'var\(--mc-content-width, 900px\)' \}\}>/,
  WorkflowProgressBar: /<div className="px-4 mx-auto w-full relative z-\[(\d+)\]" style=\{\{ maxWidth: 'var\(--mc-content-width, 900px\)' \}\}>/,
  SubagentDeliveryProgress: /className="relative z-\[(\d+)\] mx-auto w-full px-4"/,
  QueueStack: /className="px-4 mx-auto w-full relative" style=\{\{ maxWidth: 'var\(--mc-content-width, 900px\)', zIndex: (\d+) \}\}/,
}
const FILES: Record<keyof typeof CHILDREN | string, string> = {
  TaskProgressBar: 'pages/chat/TaskProgressBar.tsx',
  SubagentProgressBar: 'pages/chat/SubagentProgressBar.tsx',
  WorkflowProgressBar: 'pages/chat/WorkflowProgressBar.tsx',
  SubagentDeliveryProgress: 'components/QueueStack.tsx',
  QueueStack: 'components/QueueStack.tsx',
}

describe('composer status stack outranks the transcript bottom mask', () => {
  it('anchors the invariant: every value it compares was actually found', () => {
    // A regex that silently stopped matching would make the ordering assertions
    // below pass on `undefined > undefined` vacuity, so failing loudly here is
    // what gives the rest of the file its meaning.
    expect(MASK_Z, 'transcript bottom mask className not found in ChatPageView.tsx').not.toBeNull()
    expect(COMPOSER_Z, 'inputAreaRef wrapper not found in ChatPageView.tsx').not.toBeNull()
    expect(STACK, 'composer-status-stack block not found in ChatPageView.tsx').not.toBeNull()
  })

  it('renders exactly the children this file has checked, and no others', () => {
    // The list IS the guard. A new bar added to the stack lands here first, and
    // has to declare its own z-index before this file will go green again.
    const rendered = [...STACK![2].matchAll(/<([A-Z][A-Za-z]*)\b/g)].map(m => m[1])
    expect(new Set(rendered)).toEqual(new Set(Object.keys(CHILDREN)))
  })

  for (const [name, re] of Object.entries(CHILDREN)) {
    it(`${name} paints above the mask's overshoot`, () => {
      const m = re.exec(SRC(FILES[name]))
      expect(m, `${name}'s outermost wrapper did not match its pinned shape`).not.toBeNull()
      expect(Number(m![1])).toBeGreaterThan(Number(MASK_Z![1]))
    })
  }

  it('keeps QueueStack below the composer, so its fuse slides under the input box', () => {
    // QueueStack is the one child that OVERLAPS the composer: the collapsed front
    // card carries a -OVERLAP margin so it fuses with the input box, and at or
    // above the composer's layer it would surface ON TOP of the box instead.
    //
    // Deliberately not asserted for the rest. They are flex-flow siblings that end
    // where the composer begins, so their z-index against it never decides a pixel
    // — and SubagentProgressBar's is legitimately 46, above the composer, because
    // it has to clear theme-experience overlays. Requiring < 10 of every child
    // would read as an invariant while really just pinning that accident.
    const m = CHILDREN.QueueStack.exec(SRC(FILES.QueueStack))
    expect(Number(m![1])).toBeLessThan(Number(COMPOSER_Z![1]))
  })

  it('leaves the stack wrapper without a z-index, so it forms no stacking context', () => {
    // One z-index here would fix all five children at once and is the tempting
    // shortcut — but it would also confine SubagentProgressBar's `z-[46]`, which
    // exists to clear theme-experience overlays rendered OUTSIDE this subtree.
    // Positioning it is equally disqualifying: `position` plus a z-index is what
    // creates the context, and a bare `relative` here invites the second half.
    expect(STACK![1]).not.toMatch(/\bz-\[?\d/)
    expect(STACK![1]).not.toMatch(/\b(relative|absolute|fixed|sticky)\b/)
  })
})
