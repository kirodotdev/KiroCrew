/**
 * ChatPage scroll-shell RENDER pins — the DOM half of the characterization net.
 *
 * The recipe suite (ChatPage.scrollShell.recipe.test.tsx) pins the shell's
 * source tokens; this suite pins what those tokens must DO when mounted:
 * the scroller's child order (slots included), the older-messages spinner's
 * mount condition, and the ref/spacer wiring. It also slices the page's
 * <TranscriptScrollShell> invocation and pins WHICH slot each piece of page
 * content lives in — the cross-file seam the source-token pins cannot see
 * (swapping aboveRows/belowRows bodies keeps every toContain green; it does
 * not keep these pins green).
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import React from 'react'
import TranscriptScrollShell from '../pages/chat/TranscriptScrollShell'
import { initI18n } from '../i18n'

initI18n('en')

const ref = () => ({ current: null as HTMLDivElement | null })

function mount(loadingOlder: boolean) {
  const scrollerRef = ref()
  const virt = { topSentinelRef: ref(), bottomSentinelRef: ref(), offsetBefore: 123, offsetAfter: 456 }
  const utils = render(
    <TranscriptScrollShell
      scrollerRef={scrollerRef}
      onScroll={() => {}}
      virt={virt}
      loadingOlder={loadingOlder}
      scrollerStyle={{ paddingBottom: 16 }}
      aboveRows={<div data-testid="slot-above" />}
      belowRows={<div data-testid="slot-below" />}
    >
      <div data-testid="slot-rows" />
    </TranscriptScrollShell>,
  )
  return { scrollerRef, virt, ...utils }
}

describe('TranscriptScrollShell DOM contract', () => {
  it('renders the skeleton in order: header spacer, aboveRows, top sentinel, top spacer, rows, bottom spacer, bottom sentinel, trailing wrapper (belowRows)', () => {
    const { scrollerRef, virt } = mount(false)
    const scroller = scrollerRef.current!
    expect(scroller).toBeTruthy()
    const kids = Array.from(scroller.children)
    const at = (el: Element | null) => kids.indexOf(el as Element)

    const headerSpacer = kids.find(k => k.className.includes('h-16'))!
    const above = screen.getByTestId('slot-above')
    const topSentinel = virt.topSentinelRef.current!
    const rows = screen.getByTestId('slot-rows')
    const bottomSentinel = virt.bottomSentinelRef.current!
    // belowRows is not a direct child any more: it sits inside the trailing
    // wrapper the virtualizer observes (chrome below the rows must be seen by
    // the resize observer, and only one block can be handed to it). The wrapper
    // is the direct child whose place in the order is pinned here.
    const trailing = scroller.querySelector('[data-vc-trailing]') as HTMLElement
    expect(trailing).toBeTruthy()
    expect(trailing.contains(screen.getByTestId('slot-below'))).toBe(true)
    const topSpacer = kids.find(k => (k as HTMLElement).style.height === '123px')!
    const bottomSpacer = kids.find(k => (k as HTMLElement).style.height === '456px')!

    // Every piece must be a DIRECT child of the scroller...
    for (const el of [headerSpacer, above, topSentinel, topSpacer, rows, bottomSpacer, bottomSentinel, trailing]) {
      expect(at(el)).toBeGreaterThanOrEqual(0)
    }
    // ...in exactly this order. This is the invariant the virtualizer's
    // sentinel/spacer geometry depends on, and the one a slot swap breaks.
    const order = [headerSpacer, above, topSentinel, topSpacer, rows, bottomSpacer, bottomSentinel, trailing].map(at)
    expect(order).toEqual([...order].sort((a, b) => a - b))
    expect(new Set(order).size).toBe(order.length)
  })

  it('renders the top spacer at a whole-pixel height within half a pixel of the virtualizer offset', () => {
    // Chromium's scroll anchoring moves scrollTop only by whole pixels. A
    // fractional top spacer lets each re-price of unmounted rows above a reader
    // sitting still (a measure-farm batch) shift the visible text by the
    // leftover fraction.
    const scrollerRef = ref()
    render(
      <TranscriptScrollShell
        scrollerRef={scrollerRef}
        onScroll={() => {}}
        virt={{ topSentinelRef: ref(), bottomSentinelRef: ref(), offsetBefore: 19479.75, offsetAfter: 0 }}
        loadingOlder={false}
      >
        <div />
      </TranscriptScrollShell>,
    )
    const topSpacer = scrollerRef.current!.querySelector('.vc-spacer-skeleton') as HTMLElement
    const height = Number.parseFloat(topSpacer.style.height)
    expect(Number.isInteger(height)).toBe(true)
    expect(Math.abs(height - 19479.75)).toBeLessThanOrEqual(0.5)
  })

  it('top-down: keeps live chrome before the top sentinel and history chrome after the bottom sentinel', () => {
    const scrollerRef = ref()
    const leadingRef = ref()
    const trailingRef = ref()
    const topSentinelRef = ref()
    const bottomSentinelRef = ref()
    render(
      <TranscriptScrollShell
        scrollerRef={scrollerRef}
        onScroll={() => {}}
        virt={{ topSentinelRef, bottomSentinelRef, leadingRef, trailingRef, offsetBefore: 0, offsetAfter: 0 }}
        loadingOlder={false}
        liveEdgeAtTop
        aboveRows={<div data-testid="slot-live" />}
        belowRows={<div data-testid="slot-history" />}
      >
        <div />
      </TranscriptScrollShell>,
    )
    const leading = scrollerRef.current!.querySelector('[data-vc-leading]')
    const trailing = scrollerRef.current!.querySelector('[data-vc-trailing]')
    expect(leadingRef.current).toBe(leading)
    expect(trailingRef.current).toBe(trailing)
    expect(leading!.contains(screen.getByTestId('slot-live'))).toBe(true)
    expect(trailing!.contains(screen.getByTestId('slot-history'))).toBe(true)
    const kids = Array.from(scrollerRef.current!.children)
    expect(kids.indexOf(leading!)).toBeLessThan(kids.indexOf(topSentinelRef.current!))
    expect(kids.indexOf(trailing!)).toBeGreaterThan(kids.indexOf(bottomSentinelRef.current!))
  })

  it('bottom layout: renders no leading wrapper, so the DOM matches the layout without top-down', () => {
    const scrollerRef = ref()
    const leadingRef = ref()
    render(
      <TranscriptScrollShell
        scrollerRef={scrollerRef}
        onScroll={() => {}}
        virt={{ topSentinelRef: ref(), bottomSentinelRef: ref(), leadingRef, offsetBefore: 0, offsetAfter: 0 }}
        loadingOlder={false}
        aboveRows={<div data-testid="slot-above" />}
      >
        <div />
      </TranscriptScrollShell>,
    )
    expect(scrollerRef.current!.querySelector('[data-vc-leading]')).toBeNull()
    expect(leadingRef.current).toBeNull()
    expect(screen.getByTestId('slot-above').parentElement).toBe(scrollerRef.current)
  })

  it('hands the trailing wrapper to the virtualizer through virt.trailingRef', () => {
    const scrollerRef = ref()
    const trailingRef = ref()
    render(
      <TranscriptScrollShell
        scrollerRef={scrollerRef}
        onScroll={() => {}}
        virt={{ topSentinelRef: ref(), bottomSentinelRef: ref(), trailingRef, offsetBefore: 0, offsetAfter: 0 }}
        loadingOlder={false}
        belowRows={<div data-testid="slot-below" />}
      >
        <div />
      </TranscriptScrollShell>,
    )
    expect(trailingRef.current).toBe(scrollerRef.current!.querySelector('[data-vc-trailing]'))
    expect(trailingRef.current!.contains(screen.getByTestId('slot-below'))).toBe(true)
  })

  it('mounts the older-messages spinner between the top sentinel and the top spacer only while loadingOlder', () => {
    const off = mount(false)
    expect(off.queryByTestId('older-messages-loading')).toBeNull()
    off.unmount()

    const { scrollerRef, virt, queryByTestId } = mount(true)
    const spinner = queryByTestId('older-messages-loading')!
    expect(spinner).toBeTruthy()
    const kids = Array.from(scrollerRef.current!.children)
    const spinnerIdx = kids.indexOf(spinner)
    const sentinelIdx = kids.indexOf(virt.topSentinelRef.current!)
    const topSpacerIdx = kids.findIndex(k => (k as HTMLElement).style.height === '123px')
    expect(spinnerIdx).toBeGreaterThan(sentinelIdx)
    expect(spinnerIdx).toBeLessThan(topSpacerIdx)
  })

  it('fires onScroll from the scroller element itself', () => {
    // A handler that merely EXISTS in source can sit on the wrong element;
    // prove the scroller's own scroll event reaches the host callback.
    const scrollerRef = ref()
    let calls = 0
    render(
      <TranscriptScrollShell
        scrollerRef={scrollerRef}
        onScroll={() => { calls++ }}
        virt={{ topSentinelRef: ref(), bottomSentinelRef: ref(), offsetBefore: 0, offsetAfter: 0 }}
        loadingOlder={false}
      >
        <div />
      </TranscriptScrollShell>,
    )
    fireEvent.scroll(scrollerRef.current!)
    expect(calls).toBe(1)
  })

  it('keeps the shell-owned scroll contract even when the host tries to override it via scrollerStyle', () => {
    const scrollerRef = ref()
    render(
      <TranscriptScrollShell
        scrollerRef={scrollerRef}
        onScroll={() => {}}
        virt={{ topSentinelRef: ref(), bottomSentinelRef: ref(), offsetBefore: 0, offsetAfter: 0 }}
        loadingOlder={false}
        scrollerStyle={{ overflowX: 'auto', overflowY: 'visible', paddingBottom: 16 } as React.CSSProperties}
      >
        <div />
      </TranscriptScrollShell>,
    )
    const s = scrollerRef.current!.style
    // Shell tokens win (spread-first): the scroll contract is not overridable.
    expect(s.overflowX).toBe('hidden')
    expect(s.overflowY).toBe('auto')
    // Host-added geometry the shell does not claim still lands.
    expect(s.paddingBottom).toBe('16px')
  })
})

describe('ChatPage invocation: slot membership and prop threading', () => {
  // ChatPage composes owners under pages/chat/page/ (and its useChatPage* siblings), so the pins read the page and those owners.
  const src = [
    readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8'),
    ...readdirSync(resolve(__dirname, '../pages/chat/page'))
      .filter(f => /\.tsx?$/.test(f) && !/\.test\./.test(f))
      .sort()
      .map(f => readFileSync(resolve(__dirname, '../pages/chat/page', f), 'utf8')),
    ...readdirSync(resolve(__dirname, '../pages'))
      .filter(f => /^useChatPage.*\.ts$/.test(f) && !/\.test\./.test(f))
      .sort()
      .map(f => readFileSync(resolve(__dirname, '../pages', f), 'utf8')),
  ].join('\n')
  const open = src.indexOf('<TranscriptScrollShell')
  const close = src.indexOf('</TranscriptScrollShell>')
  const inv = src.slice(open, close)

  it('slices exactly one invocation', () => {
    expect(open).toBeGreaterThan(0)
    expect(close).toBeGreaterThan(open)
    expect(src.indexOf('<TranscriptScrollShell', open + 1)).toBe(-1)
  })

  it('threads every wiring prop exactly once', () => {
    for (const pin of [
      'scrollerRef={scrollerRef}',
      'onScroll={onScrollPin}',
      'virt={virt}',
      'loadingOlder={loadingOlder}',
      // PREFIXES, not the whole literal: the style object also carries the
      // restore-gate visibility flip, so pinning the closing braces would pin the
      // gate's presence into a test about prop THREADING. This still fails on a
      // duplicated prop and still requires the padding the shell contract needs:
      // the dock's strip at the bottom by default, at the top in the opt-in
      // top-down layout.
      'scrollerStyle={topDown',
      '? { paddingTop: dockTop + dockH + DOCK_CLEARANCE_PX',
      ': { paddingBottom: dockH + DOCK_CLEARANCE_PX',
    ]) {
      expect(inv.split(pin).length - 1, pin).toBe(1)
    }
  })

  it('keeps page content in named slots and swaps those slots only for top-down', () => {
    const historyStart = src.indexOf('const historySlot = <>')
    const liveStart = src.indexOf('const liveSlot = <>', historyStart)
    const returnStart = src.indexOf('\n\n  return (', liveStart)
    expect(historyStart).toBeGreaterThan(-1)
    expect(liveStart).toBeGreaterThan(historyStart)
    expect(returnStart).toBeGreaterThan(liveStart)
    const history = src.slice(historyStart, liveStart)
    const live = src.slice(liveStart, returnStart)

    expect(history).toContain('slotHasMore && cursorIsForActiveSlot')
    expect(history).toContain('<EarlierMessagesBar')
    expect(history).not.toContain('<ChatFooter')

    const iFooter = live.indexOf('<ChatFooter')
    const iSurvey = live.indexOf('<SessionPulseSurveyCard')
    const iTail = live.indexOf('height: TRANSCRIPT_TAIL_SPACER_PX')
    expect(iFooter).toBeGreaterThan(-1)
    expect(iSurvey).toBeGreaterThan(iFooter)
    expect(iTail).toBeGreaterThan(iSurvey)
    expect(live).not.toContain('<EarlierMessagesBar')

    expect(inv).toContain('liveEdgeAtTop={topDown}')
    expect(inv).not.toContain('historyAtBottom')
    expect(inv).toContain('aboveRows={topDown ? liveSlot : historySlot}')
    expect(inv).toContain('belowRows={topDown ? historySlot : liveSlot}')
    for (const token of ['<EarlierMessagesBar', '<ChatFooter', '<SessionPulseSurveyCard', 'height: TRANSCRIPT_TAIL_SPACER_PX']) {
      expect(inv, `shell children must not contain ${token}`).not.toContain(token)
    }
  })
})
