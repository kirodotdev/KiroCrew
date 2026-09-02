import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

// Comments are stripped before matching: the rules below are explained in prose
// that quotes the very class names being asserted against, and a raw-text
// negative match hits the explanation instead of the code.
const src = async () => {
  const raw = await readFile(join(__dirname, '..', 'pages', 'ChannelPage.tsx'), 'utf8')
  return raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

describe('ChannelPage narrow viewport', () => {
  it('drives its panes from the shared list-detail hook', async () => {
    const s = await src()
    expect(s).toContain('useListDetailView()')
    // Both flags must be consumed. Taking only one produces a shell that can
    // enter the detail and never return, or vice versa.
    expect(s).toMatch(/showList/)
    expect(s).toMatch(/showDetail/)
  })

  it('gives the channel list the full width instead of a 256px slice', async () => {
    const s = await src()
    expect(s).toMatch(/isMobile \? 'w-full' : 'w-64 shrink-0 border-r border-border'/)
    // An unconditional w-64 is the defect: measured, it left the transcript 86px.
    expect(s).not.toMatch(/className="w-64 shrink-0 border-r border-border flex flex-col"/)
  })

  it('lets the transcript own the viewport when it is the visible pane', async () => {
    const s = await src()
    expect(s).toMatch(/showDetail \? 'flex' : 'hidden'/)
    expect(s).not.toMatch(/className="flex-1 flex flex-col min-w-0"/)
  })

  it('carries a way back, so drilling in is not a one-way door', async () => {
    const s = await src()
    expect(s).toContain('ListDetailBack')
    expect(s).toMatch(/onBack=\{closeDetail\}/)
  })

  it('collapses the list when a channel is picked', async () => {
    const s = await src()
    // Without this the list keeps the viewport and the transcript never shows,
    // which would make the whole change inert on a phone.
    expect(s).toMatch(/setActiveId\(ch\.id\); openDetail\(\)/)
  })

  it('wraps the transcript header so Back cannot overlap the controls', async () => {
    const s = await src()
    // The button group is shrink-0 and ~300px wide. With min-w-0 alone the title
    // group collapsed to zero and its shrink-0 Back button overflowed its own
    // parent, painting over those buttons; basis-full puts them on separate lines.
    expect(s).toMatch(/flex flex-wrap items-center justify-between gap-x-2 gap-y-1/)
    expect(s).toMatch(/flex flex-1 items-center gap-1 min-w-0 basis-full sm:basis-auto/)
    expect(s, 'a non-wrapping header is the overlap')
      .not.toMatch(/py-2\.5 flex items-center justify-between gap-2"/)
  })

  it('consumes the shared auto-grow hook instead of a second spelling', async () => {
    const s = await src()
    // The private copy latched a zero measured inside the hidden pane. The guard
    // belongs in the hook, whose five other call sites have the same exposure.
    expect(s).toMatch(/useAutoGrowTextarea\(ref, value, Math\.round\(window\.innerHeight \* 0\.3\)\)/)
    expect(s, 'the hand-rolled copy must be gone').not.toMatch(/const applyHeight = \(\) =>/)
  })

  it('leaves the composer height unfloored, so the desktop keeps its own box', async () => {
    const s = await src()
    // An unconditional min-h-11 took the desktop composer from 36px to 44px, which
    // this change never set out to do. The guard already leaves the natural rows=1
    // box and the hook's re-measure handles a two-line placeholder.
    expect(s).not.toMatch(/min-h-11/)
  })

  it('keeps the standard content container and full-bleeds inside the pane', async () => {
    const s = await src()
    // `page-layout-pattern` names this container literally, and its stated harm is a
    // max-width wrapper breaking `overflow-y-auto`, not the gutter. So the container
    // stays as written and the narrow pane cancels the gutter itself. The pull-back
    // is pinned to the gutter it cancels: -mx-4 against the recommended 16px narrow
    // gutter. Widening one without the other pushes the pane past the screen edge,
    // which overflows nothing on a per-character-breaking script and so is invisible
    // to a scroll assertion.
    expect(s).toMatch(/className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0"/)
    expect(s).toMatch(/flex h-full relative \$\{isMobile \? '-mx-4 -mb-8' : ''\}/)
    expect(s, 'gating the container itself is what the rule names')
      .not.toMatch(/isMobile \? 'px-0 pb-0' : 'px-6 pb-8'/)
    expect(s).toMatch(/isMobile \? 'px-0' : 'px-2'/)
  })

  it('returns to the list when the channel is closed', async () => {
    const s = await src()
    // Without this the transcript pane stays on with no channel: the list holding
    // "+ New" is hidden and ListDetailBack unmounted with the channel, so there is
    // no way out short of a reload.
    expect(s).toMatch(/setActiveId\(null\)\s*\n\s*closeDetail\(\)/)
  })

  it('drills into a channel it has just created', async () => {
    const s = await src()
    expect(s).toMatch(/setActiveId\(res\.channel\.id\)\s*\n\s*openDetail\(\)/)
  })

  it('keeps Reply visible where there is no hover', async () => {
    const s = await src()
    // Hover-revealed is the only entry point to a thread; a touch viewport has none.
    expect(s).toMatch(/md:opacity-0 md:group-hover:opacity-100/)
    expect(s, 'an ungated hover reveal is the defect')
      .not.toMatch(/text-muted hover:text-text opacity-0 group-hover:opacity-100/)
  })

  it('yields the message list and the channel composer to an open thread', async () => {
    const s = await src()
    // The thread renders through DetailPanel, which already takes the full width
    // while narrow, and carries its own composer with the same placeholder -- two
    // identical boxes stacked means replying into the wrong one.
    expect(s).toMatch(/isMobile && \(showAgents \|\| threadId\) \? 'hidden' : ''/)
    // Widened deliberately: the composer now also stands down for the agents panel,
    // which hides the transcript the same way a thread does.
    expect(s).toMatch(/border-t border-border px-4 py-3 \$\{isMobile && \(threadId \|\| showAgents\) \? 'hidden' : ''\}/)
  })

  it('stores one draft per thread', async () => {
    const s = await src()
    // Three review rounds each produced a counter-example to a single shared draft
    // box: clearing it on a channel change lost work, clearing it when switching
    // threads lost work, and not clearing it handed one thread's text to another. A
    // map makes all three unreachable and removes the two guards that tried to.
    expect(s).toMatch(/const \[threadDrafts, setThreadDrafts\] = useState<Record<string, string>>/)
    expect(s).toMatch(/const threadInput = threadId \? \(threadDrafts\[threadId\] \?\? ''\) : ''/)
    expect(s, 'a single shared draft string is the defect')
      .not.toMatch(/const \[threadInput, setThreadInput\] = useState/)
    expect(s, 'the owner-id comparison is what the map replaces')
      .not.toMatch(/threadDraftId/)
    expect(s).toMatch(/onReply=\{\(\) => openThread\(msg\.id\)\}/)
    expect(s).toMatch(/onOpenThread=\{\(\) => openThread\(msg\.id\)\}/)
  })

  it('discards a draft on send only, never on closing the panel', async () => {
    const s = await src()
    // On a phone the thread overlay hides the transcript, so re-reading the
    // conversation requires closing the panel. Discarding there charged the user their
    // draft for looking something up, and the agents-toggle exit kept it -- two exits,
    // two behaviours. The draft is keyed by thread, so keeping it leaks nothing.
    expect(s).toMatch(/onClose=\{\(\) => setThreadId\(null\)\}/)
    expect(s, 'closing must not destroy the reply being written')
      .not.toMatch(/setThreadId\(null\); if \(id\) discardThreadDraft/)
    expect(s).toMatch(/await sendMessage\(threadInput, threadId\)\s*\n\s*discardThreadDraft\(threadId\)/)
  })

  it('clears overlay state when the channel changes', async () => {
    const s = await src()
    // A thread id names a message in ONE channel, and the thread panel's composer
    // sends against whatever channel is ACTIVE -- so a stale id parents a reply to a
    // message the new channel does not contain. Clearing only on Back would miss
    // every other route that changes activeId.
    expect(s).toMatch(/setThreadId\(null\)\s*\n\s*setShowAgents\(false\)\s*\n\s*\}, \[activeId\]\)/)
  })

  it('keeps the two overlays mutually exclusive', async () => {
    const s = await src()
    // Both render in the same flex row at w-full, so opening the second splits the
    // viewport into two ~190px columns -- the squeeze this change exists to remove.
    // Gated on the narrow case: the counted harm -- two ~190px columns -- exists only
    // where both overlays are `w-full`. A desktop keeps them side by side, and removing
    // that was a capability loss nobody named.
    expect(s).toMatch(/setShowAgents\(v => !v\); if \(isMobile\) setThreadId\(null\)/)
    expect(s).toMatch(/const openThread = \(id: string\) => \{[\s\S]*?if \(isMobile\) setShowAgents\(false\)/)
  })

  it('stands the channel composer down for either overlay', async () => {
    const s = await src()
    // The transcript is hidden behind either one, so a sent message would land with
    // nothing visible to confirm it.
    expect(s).toMatch(/isMobile && \(threadId \|\| showAgents\) \? 'hidden' : ''/)
  })

  it('gives the agents panel the full width rather than a second 256px rail', async () => {
    const s = await src()
    expect(s).toMatch(/isMobile \? 'w-full' : 'w-64 shrink-0 border-l border-border'/)
    expect(s).not.toMatch(/className="w-64 shrink-0 border-l border-border flex flex-col bg-bg-elevated"/)
  })

  it('hides the message list with a class rather than unmounting it', async () => {
    const s = await src()
    // The composer draft and the read position live in this subtree, and a
    // rotation across the breakpoint would discard both.
    // The condition now covers a thread as well as the agents panel -- both take the
    // full width while narrow -- so the invariant is "hidden, for either overlay",
    // not "hidden for showAgents".
    expect(s).toMatch(/isMobile && \(showAgents \|\| threadId\) \? 'hidden' : ''/)
    expect(s).not.toMatch(/\{!showAgents && <div className="flex-1 overflow-y-auto/)
    expect(s, 'unmounting would discard the read position')
      .not.toMatch(/\{!\(isMobile && threadId\) && \(\s*<MentionInput/)
  })

  it('does not position the agents panel absolutely, having no positioned ancestor', async () => {
    const s = await src()
    // `absolute inset-0` here would resolve against whatever is positioned further
    // up the tree, not this row. Plain flex is what actually contains it.
    expect(s).not.toMatch(/absolute inset-0[^']*'\s*:\s*'w-64 shrink-0 border-l/)
  })

  it('lets the composer input shrink so the trailing Send button stays visible', async () => {
    const s = await src()
    // A flex item defaults to `min-width: auto`, which in engines that honor a
    // form control's intrinsic floor through a wrapper can stop a flex-1 input
    // wrapper from shrinking, overflowing the row and clipping the trailing
    // Send button. min-w-0 lifts the floor unconditionally, matching the two
    // sibling wrappers in this file that already carry it. Measured: the clip
    // does not reproduce in Chromium or Firefox (the wrapper's flex basis is 0
    // and the w-full textarea adds no min-content floor through it) — this
    // pins alignment with the siblings, not an observed defect.
    expect(s).toMatch(/className="relative flex-1 min-w-0"/)
  })
})
