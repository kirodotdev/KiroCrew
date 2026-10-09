import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * The composer dock is one positioned box floating over the bottom of the
 * transcript scroller, and everything in it — the status stack's bars, the queue
 * cards, the composer — is layered by explicit z-indexes. Three things have to
 * stay true for those layers to mean anything, and each is cheap to lose:
 *
 *  1. Neither the dock root nor the status-stack wrapper may form a stacking
 *     context. One z-index on either would be the tempting shortcut that fixes
 *     every child at once — and would also CONFINE SubagentProgressBar's `z-[46]`,
 *     which exists to clear theme-experience overlays rendered OUTSIDE this
 *     subtree. Positioning plus z-index is what creates the context; the dock
 *     root is positioned (it has to float), so its z-index must stay `auto`.
 *  2. QueueStack stays below the composer's own layer: the collapsed front card
 *     carries a -OVERLAP margin so it fuses with the input box, and at or above
 *     the composer's z-index it would surface ON TOP of the box instead.
 *  3. The stack renders exactly the children this file has checked. A sixth bar
 *     lands here first and has to declare its own layer before this goes green.
 *
 * Asserted against SOURCE TEXT: the numbers live in five files, several as
 * Tailwind classes jsdom cannot resolve into a paint order, and the invariant is
 * the comparison BETWEEN them.
 */
const SRC = (p: string) => readFileSync(resolve(__dirname, '..', p), 'utf8')
const CHAT_PAGE = SRC('pages/ChatPage.tsx')

/** The dock root: the one positioned box that floats over the scroller. */
const DOCK = /<div ref=\{dockRef\} className="([^"]*)" style=\{\{ right: dockGutter \}\} data-testid="composer-dock-root"[^>]*>/.exec(CHAT_PAGE)
/** The composer's own layer. A bar at or above it would paint over the input box,
 *  and QueueStack's -OVERLAP fuse would surface ON TOP of the composer. */
const COMPOSER_Z = /<div ref=\{inputAreaRef\} className="relative z-(\d+) dock-inert">/.exec(CHAT_PAGE)
/** The stack wrapper's own className, and the JSX block it encloses. */
const STACK = /<div ref=\{composerBandRef\} className="([^"]*)" data-testid="composer-status-stack">([\s\S]*?)\n {14}<\/div>/.exec(CHAT_PAGE)

/** Every component the stack renders, paired with the z-index its own outermost
 *  wrapper declares. Each value is read out of that component's real source. */
const CHILDREN: Record<string, RegExp> = {
  CommandCenterDock: /<div className="px-4 mx-auto w-full relative z-\[(\d+)\]" style=\{\{ maxWidth: 'var\(--mc-input-width, 900px\)' \}\}>/,
  TaskProgressBar: /<div className="px-4 mx-auto w-full relative z-\[(\d+)\]" style=\{\{ maxWidth: 'var\(--mc-content-width, 900px\)' \}\}>/,
  SubagentProgressBar: /<div className="px-4 mx-auto w-full relative z-\[(\d+)\]" style=\{\{ maxWidth: 'var\(--mc-content-width, 900px\)' \}\}>/,
  WorkflowProgressBar: /<div className="px-4 mx-auto w-full relative z-\[(\d+)\]" style=\{\{ maxWidth: 'var\(--mc-content-width, 900px\)' \}\}>/,
  SubagentDeliveryProgress: /className="relative z-\[(\d+)\] mx-auto w-full px-4"/,
  QueueStack: /className="px-4 mx-auto w-full relative" style=\{\{ maxWidth: 'var\(--mc-content-width, 900px\)', zIndex: (\d+) \}\}/,
}
const FILES: Record<keyof typeof CHILDREN | string, string> = {
  CommandCenterDock: 'pages/chat/command-center/CommandCenterDock.tsx',
  TaskProgressBar: 'pages/chat/TaskProgressBar.tsx',
  SubagentProgressBar: 'pages/chat/SubagentProgressBar.tsx',
  WorkflowProgressBar: 'pages/chat/WorkflowProgressBar.tsx',
  SubagentDeliveryProgress: 'components/QueueStack.tsx',
  QueueStack: 'components/QueueStack.tsx',
}

describe('composer dock layering', () => {
  it('anchors the invariant: every value it compares was actually found', () => {
    // A regex that silently stopped matching would make the assertions below
    // pass on `undefined` vacuity, so failing loudly here is what gives the rest
    // of the file its meaning.
    expect(DOCK, 'composer-dock-root not found in ChatPage.tsx').not.toBeNull()
    expect(COMPOSER_Z, 'inputAreaRef wrapper not found in ChatPage.tsx').not.toBeNull()
    expect(STACK, 'composer-status-stack block not found in ChatPage.tsx').not.toBeNull()
  })

  it('renders exactly the children this file has checked, and no others', () => {
    const rendered = [...STACK![2].matchAll(/<([A-Z][A-Za-z]*)\b/g)].map(m => m[1])
    expect(new Set(rendered)).toEqual(new Set(Object.keys(CHILDREN)))
  })

  for (const [name, re] of Object.entries(CHILDREN)) {
    it(`${name} declares its own layer`, () => {
      const m = re.exec(SRC(FILES[name]))
      expect(m, `${name}'s outermost wrapper did not match its pinned shape`).not.toBeNull()
      expect(Number(m![1])).toBeGreaterThan(0)
    })
  }

  it('keeps QueueStack below the composer, so its fuse slides under the input box', () => {
    // Deliberately not asserted for the rest: they end where the composer
    // begins, so their z-index against it never decides a pixel — and
    // SubagentProgressBar's is legitimately 46, above the composer, because it
    // has to clear theme-experience overlays.
    const m = CHILDREN.QueueStack.exec(SRC(FILES.QueueStack))
    expect(Number(m![1])).toBeLessThan(Number(COMPOSER_Z![1]))
  })

  it('leaves the stack wrapper without a z-index or position, so it forms no stacking context', () => {
    expect(STACK![1]).not.toMatch(/\bz-\[?\d/)
    expect(STACK![1]).not.toMatch(/\b(relative|absolute|fixed|sticky)\b/)
  })

  it('floats the dock root with z-index auto, so it forms no stacking context either', () => {
    // It MUST be positioned (that is what makes it float over the scroller), so
    // the only thing keeping it from becoming a context is the absent z-index.
    expect(DOCK![1]).toMatch(/\babsolute\b/)
    expect(DOCK![1]).toMatch(/\bbottom-0\b/)
    expect(DOCK![1]).not.toMatch(/\bz-\[?\d/)
  })

  it('lets input fall through the dock root and only the control boxes catch it', () => {
    // The root and both wrapper boxes span the pane's full width; only their
    // children — each its own content column — catch input, so the empty
    // width beside the column scrolls the transcript beneath.
    expect(DOCK![1]).toMatch(/\bpointer-events-none\b/)
    expect(STACK![1]).toMatch(/\bdock-inert\b/)
    expect(STACK![1]).not.toMatch(/\bpointer-events-auto\b/)
    expect(CHAT_PAGE).toMatch(/<div ref=\{inputAreaRef\} className="relative z-\d+ dock-inert">/)
    expect(SRC('index.css')).toMatch(/\.dock-inert\{pointer-events:none\}\.dock-inert>\*\{pointer-events:auto\}/)
    // The stack is a scroll box: while it overflows its cap, its own scrollbar
    // must be a hit target, and a scrollbar belongs to the box, not a child. The
    // band observer marks the crossing and the stylesheet lifts the pass-through.
    expect(SRC('index.css')).toMatch(/\.dock-inert\[data-overflowing="true"\]\{pointer-events:auto\}/)
    expect(SRC('pages/chat/useChatPageTranscriptController.tsx')).toMatch(/el\.dataset\.overflowing = el\.scrollHeight > el\.clientHeight \? 'true' : 'false'/)
  })
})
