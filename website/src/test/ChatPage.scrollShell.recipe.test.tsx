// Characterization net for ChatPage's transcript SCROLL SHELL — the region a
// later refactor moves behind the shared chat scroll components. Written
// against the CURRENT page and required to pass unmodified before and after
// that move: these tests describe reality, not intention, and editing them in
// the migration PR is itself a review red flag.
//
// Asserted against SOURCE TEXT (the fadeClearance suite's technique) because
// most of the shell's load-bearing contracts are invisible to jsdom: it has no
// layout engine, so `overscrollBehavior`, `scrollbarGutter`, gradient
// geometry, and element ORDER inside the scroller can only be pinned where
// they are written. Each assertion cites why the token is load-bearing, so a
// legitimate future change knows what it is trading away.
//
// Companion: ChatPage.scrollShell.render.test.tsx pins the subset that IS
// meaningful to render; scripts/mutation-check-scroll-shell.mjs proves the two
// files together go red for every meaningful line mutation in the region.

import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// The shell's source may live inline in ChatPage or in extracted components —
// the net follows the CODE, not the file: assertions are identical either way,
// and a migration retargets nothing here.
const SHELL_FILES = [
  '../pages/ChatPage.tsx',
  '../pages/chat/TranscriptScrollShell.tsx', // post-extraction home (absent pre-extraction)
  '../app-sdk/ChatScrollChrome.tsx',
]
const SRC = SHELL_FILES.map((f) => {
  // Fail LOUD on a missing file: a swallowed read would let a rename of any
  // shell file silently shrink every pin in this suite to the remaining files.
  return readFileSync(resolve(__dirname, f), 'utf8')
}).join('\n// ---- file boundary ----\n')

/** Slice the source between two unique anchors (inclusive of neither).
 *  Failing to find an anchor is itself a pin: renaming or deleting the
 *  anchored construct must turn this suite red, not silently shrink it. */
function between(startAnchor: string | RegExp, endAnchor: string | RegExp): string {
  const startMatch = typeof startAnchor === 'string' ? { index: SRC.indexOf(startAnchor), len: startAnchor.length } : (() => { const m = startAnchor.exec(SRC); return m ? { index: m.index, len: m[0].length } : { index: -1, len: 0 } })()
  expect(startMatch.index, `start anchor not found: ${startAnchor}`).toBeGreaterThanOrEqual(0)
  const rest = SRC.slice(startMatch.index + startMatch.len)
  const endIdx = typeof endAnchor === 'string' ? rest.indexOf(endAnchor) : (endAnchor.exec(rest)?.index ?? -1)
  expect(endIdx, `end anchor not found after start: ${endAnchor}`).toBeGreaterThanOrEqual(0)
  return rest.slice(0, endIdx)
}

/** Pins match against the concatenated shell sources: every pinned token is
 *  unique across them, so WHERE the code lives is free to change while WHAT
 *  it says is not. Ordering assertions hold because each ordered group lives
 *  (and moves) together in one file. */
const SHELL = SRC

describe('scroll shell: header fade', () => {
  it('mounts the shared top fade anchored to the header row bottom edge', () => {
    // anchor="below" (not the overlay default): in-flow or top-anchored
    // variants consumed 24px of layout and pushed the pinned card off the
    // header. The gradient recipe itself is pinned in ChatScrollChrome's
    // consumer suites; what belongs to THIS page is the anchored mount.
    expect(SRC).toContain('<EdgeFade side="top" anchor="below" />')
  })
})

describe('scroll shell: the scroller element contract', () => {
  const scroller = () => between('ref={scrollerRef}', '{/* Header spacer */}')

  it('is focusable for bar-unmount focus handoff without adding a tab stop', () => {
    expect(scroller()).toContain('tabIndex={-1}')
  })

  it('keeps the stable theming hook class', () => {
    // website/docs/theming-contract.md: third-party themes select on it.
    expect(scroller()).toContain('className="chat-container"')
  })

  it('owns the flexible column: flex 1 is what lets the transcript fill and shrink', () => {
    expect(scroller()).toContain('flex: 1,')
  })

  it('carries the second half of the fade-band clearance as px padding', () => {
    // Pairs with TRANSCRIPT_TAIL_SPACER_PX; unlike the tail spacer it also
    // applies to a transcript too short to scroll (fadeClearance pins the sum).
    // HOST-owned geometry: the page supplies it, wherever the scroller lives.
    expect(SRC).toContain('paddingBottom: 16')
  })

  it('pins overflow-x so one over-wide child cannot give the list a horizontal scrollbar', () => {
    expect(scroller()).toContain("overflowY: 'auto'")
    expect(scroller()).toContain("overflowX: 'hidden'")
  })

  it('reserves a stable scrollbar gutter so the thumb is not hidden behind the header', () => {
    expect(scroller()).toContain("scrollbarGutter: 'stable'")
  })

  it('keeps native scroll anchoring on for async resizes above the viewport', () => {
    expect(scroller()).toContain("overflowAnchor: 'auto'")
  })

  it('contains overscroll so edge momentum cannot drag the app shell', () => {
    expect(scroller()).toContain("overscrollBehavior: 'contain'")
  })

  it('is an accessible live region wired to the pin-scroll handler', () => {
    expect(scroller()).toContain("aria-label={i18nT('pages.chatPage.chat_messages')}")
    expect(scroller()).toContain('aria-live="polite"')
    // PAGE-owned wiring: the single scroll controller's handler reaches the
    // scroller no matter which file renders the element.
    expect(SRC).toContain('onScrollPin}')
  })
})

describe('scroll shell: element order inside the scroller', () => {
  it('keeps the SKELETON sequence that moves as one unit: header spacer, top sentinel, loading, top spacer, bottom spacer, bottom sentinel', () => {
    // Order is the contract the virtualizer's IO wiring assumes; a reorder can
    // compile, render, and still break window expansion. Anchors here are the
    // skeleton pieces that live (and move) TOGETHER, so the pin holds whether
    // the skeleton sits inline in ChatPage or in an extracted shell component.
    // Cross-boundary order (earlier bar above the rows, footer below them) is
    // pinned by ChatPage.scrollShell.render.test.tsx, which mounts the shell
    // and asserts the scroller's actual child order plus which call-site slot
    // each piece of page content lives in.
    const anchors = [
      '<div className="h-16" />',
      'ref={virt.topSentinelRef}',
      'data-testid="older-messages-loading"',
      'height: virt.offsetBefore',
      'height: virt.offsetAfter',
      'ref={virt.bottomSentinelRef}',
    ]
    let cursor = -1
    for (const a of anchors) {
      const idx = SHELL.indexOf(a, cursor + 1)
      expect(idx, `out of order or missing: ${a}`).toBeGreaterThan(cursor)
      cursor = idx
    }
  })

  it('keeps the page-owned tail composition: footer, survey width cap, px tail spacer', () => {
    expect(SHELL).toContain('<ChatFooter')
    expect(SHELL).toContain('<SessionPulseSurveyCard')
    expect(SHELL).toContain('<div style={{ height: TRANSCRIPT_TAIL_SPACER_PX }} />')
  })

  it('keeps both sentinels 1px, decorative, and bound to the virtualizer refs', () => {
    expect(SHELL).toContain('<div ref={virt.topSentinelRef} aria-hidden style={{ height: 1 }} />')
    expect(SHELL).toContain('<div ref={virt.bottomSentinelRef} aria-hidden style={{ height: 1 }} />')
  })

  it('keeps both window spacers exempt from browser scroll anchoring', () => {
    // The spacers resize as the window moves; letting the browser anchor on
    // them (rather than real content) is the mid-fetch jump this exempts.
    expect(SHELL).toContain("<div aria-hidden style={{ height: virt.offsetBefore, overflowAnchor: 'none' }} />")
    expect(SHELL).toContain("<div aria-hidden style={{ height: virt.offsetAfter, overflowAnchor: 'none' }} />")
  })

  it('gates the earlier-messages bar on the cursor belonging to the active slot', () => {
    // Mid-switch slotHasMore still describes the OUTGOING chat; the cursor key
    // gate mirrors the paging thunk's own precondition.
    expect(SHELL).toContain('{slotHasMore && cursorIsForActiveSlot && (')
  })

  it('keeps the loading spinner sticky below the header, anchor-exempt, on an opaque bg', () => {
    // Slice the WHOLE conditional block (gate to the next skeleton comment) so
    // the class is bound to the spinner element itself — a SHELL-wide contain
    // would stay green with the literal parked in a comment anywhere.
    const spinner = between('{loadingOlder && (', '{/* Top spacer')
    expect(spinner).toContain('className="sticky top-16 z-[1] flex justify-center py-2"')
    expect(spinner).toContain('data-testid="older-messages-loading"')
    expect(spinner).toContain("overflowAnchor: 'none'")
    expect(spinner).toContain("background: 'var(--bg)'")
    expect(spinner).toContain('<Loader size={16} className="animate-spin text-muted" />')
  })
})

describe('scroll shell: row wrapper contract (what the shell must pass through)', () => {
  // Rows themselves are content, but their WRAPPERS are the virtualizer's
  // render contract: identity key, measureRef, the debug index, and the
  // unmounted-row placeholder. A migration that re-wraps rows must keep these.
  it('mounts only windowed rows; unmounted rows render null (spacers stand in)', () => {
    expect(SHELL).toContain('if (!vi.mounted) return null')
  })

  it('keeps key + measureRef + data-display-index on BOTH row shapes (turn and single)', () => {
    const wrappers = SHELL.match(/key=\{vi\.key\} ref=\{virt\.measureRef\(vi\.index\)\} data-display-index=\{displayIdx\}/g) ?? []
    expect(wrappers.length).toBe(2)
  })

  it('caps single rows at the theme content width', () => {
    expect(SHELL).toContain("maxWidth: 'var(--mc-content-width, 900px)'")
  })
})

describe('scroll shell: extraction wiring (the seams the split created)', () => {
  // Each of these lines compiles away silently if deleted — the type system
  // accepts a shell that ignores a prop — but the transcript then breaks at
  // runtime (no rows, no follow, no paging). The pins make every seam a test.
  it('shell renders all three slots in skeleton order and spreads host geometry', () => {
    const shellOrder = ['{aboveRows}', 'height: virt.offsetBefore', '{children}', 'height: virt.offsetAfter', '{belowRows}']
    let cursor = -1
    for (const a of shellOrder) {
      const idx = SHELL.indexOf(a, cursor + 1)
      expect(idx, `out of order or missing: ${a}`).toBeGreaterThan(cursor)
      cursor = idx
    }
    expect(SHELL).toContain('...scrollerStyle,')
    // onScroll must be ON THE SCROLLER ELEMENT, not merely present somewhere:
    // relocated onto any other element, follow/pin/paging all die at runtime
    // while a whole-source contain() stays green. Slice the scroller's own
    // attribute span (its ref up to the first child) and pin it there. The
    // render suite additionally proves it fires (dispatch a scroll event).
    expect(between('ref={scrollerRef}', '{/* Header spacer */}')).toContain('onScroll={onScroll}')
  })

  it('page threads the controller wiring onto the shell call', () => {
    // The import IS the extraction: its deletion is the "silently undone"
    // mutation, and a collection failure alone names no pin.
    expect(SRC).toContain("import TranscriptScrollShell from './chat/TranscriptScrollShell'")
    expect(SRC).toContain('scrollerRef={scrollerRef}')
    expect(SRC).toContain('virt={virt}')
    // Two consumers thread loadingOlder (the pinned-banner row props and the
    // shell call); a bare contain() would let either deletion hide behind the
    // other, so pin the count.
    expect((SRC.match(/loadingOlder=\{loadingOlder\}/g) ?? []).length).toBeGreaterThanOrEqual(2)
  })
})

describe('scroll shell: bottom mask and jump pill', () => {
  it('keeps the measured-mask recipe (geometry itself is pinned by fadeClearance)', () => {
    expect(SHELL).toContain('className="bg-gradient-to-t from-bg from-[62%] to-transparent pointer-events-none relative z-[1]"')
    // Decorative: the mask must never be announced by a screen reader.
    const mask = between('{/* Transcript bottom mask', '<div className="relative">')
    expect(mask).toContain('aria-hidden')
  })

  it('mounts the shared jump pill: visible only away from the bottom of a non-empty transcript, jump = forced pin through the single scroll controller', () => {
    // The pill's own markup (geometry, classes, catalog label) is pinned by
    // the ChatScrollChrome consumers' suites; what belongs to THIS page is the
    // visibility gate and the controller wiring.
    expect(SRC).toContain('<JumpToBottomButton visible={!isAtBottom && messages.length > 0} onClick={() => scrollBottom(true)} />')
  })
})
