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
import { readFileSync } from 'node:fs'
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
  it('renders the skeleton in order: header spacer, aboveRows, top sentinel, top spacer, rows, bottom spacer, bottom sentinel, belowRows', () => {
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
    const below = screen.getByTestId('slot-below')
    const topSpacer = kids.find(k => (k as HTMLElement).style.height === '123px')!
    const bottomSpacer = kids.find(k => (k as HTMLElement).style.height === '456px')!

    // Every piece must be a DIRECT child of the scroller...
    for (const el of [headerSpacer, above, topSentinel, topSpacer, rows, bottomSpacer, bottomSentinel, below]) {
      expect(at(el)).toBeGreaterThanOrEqual(0)
    }
    // ...in exactly this order. This is the invariant the virtualizer's
    // sentinel/spacer geometry depends on, and the one a slot swap breaks.
    const order = [headerSpacer, above, topSentinel, topSpacer, rows, bottomSpacer, bottomSentinel, below].map(at)
    expect(order).toEqual([...order].sort((a, b) => a - b))
    expect(new Set(order).size).toBe(order.length)
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
  const src = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')
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
      'scrollerStyle={{ paddingBottom: 16 }}',
    ]) {
      expect(inv.split(pin).length - 1, pin).toBe(1)
    }
  })

  it('puts each piece of page content INSIDE its slot body — membership, not just source order', () => {
    // Slice the actual slot fragment bodies. Order-only index checks pass a
    // mutant that empties belowRows and reparents the footer as a plain child
    // before the rows; membership + negative assertions do not.
    const body = (attr: string) => {
      const start = inv.indexOf(`${attr}={<>`)
      expect(start, `${attr} fragment opens`).toBeGreaterThan(-1)
      const end = inv.indexOf('</>}', start)
      expect(end, `${attr} fragment closes`).toBeGreaterThan(start)
      return inv.slice(start, end)
    }
    const above = body('aboveRows')
    const below = body('belowRows')
    // children = everything after the opening tag closes (past both slot
    // fragments) up to </TranscriptScrollShell>.
    const childrenRegion = inv.slice(inv.indexOf('</>}', inv.indexOf('belowRows={<>')) + 4)

    // aboveRows: the paging bar and its cursor gate — and nothing footer-shaped.
    expect(above).toContain('slotHasMore && cursorIsForActiveSlot')
    expect(above).toContain('<EarlierMessagesBar')
    expect(above).not.toContain('<ChatFooter')

    // belowRows: footer -> survey -> tail spacer, in order, and no paging bar.
    const iFooter = below.indexOf('<ChatFooter')
    const iSurvey = below.indexOf('<SessionPulseSurveyCard')
    const iTail = below.indexOf('height: TRANSCRIPT_TAIL_SPACER_PX')
    expect(iFooter).toBeGreaterThan(-1)
    expect(iSurvey).toBeGreaterThan(iFooter)
    expect(iTail).toBeGreaterThan(iSurvey)
    expect(below).not.toContain('<EarlierMessagesBar')

    // The children region carries rows ONLY — reparenting any slot content
    // there (where it would enter the virtualized row geometry) is a red.
    for (const token of ['<EarlierMessagesBar', '<ChatFooter', '<SessionPulseSurveyCard', 'height: TRANSCRIPT_TAIL_SPACER_PX']) {
      expect(childrenRegion, `children region must not contain ${token}`).not.toContain(token)
    }
  })
})
