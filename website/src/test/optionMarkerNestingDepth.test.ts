/**
 * A sibling action after a BALANCED nested pair, inside a still-open head, is not a marker.
 *
 * The pairwise `lastCloser < lastHead` test could not see this: in
 * `[OPTIONS: x [OPTION-ACTIONS: a] [OPTION-ACTIONS: b]` the first pair supplies BOTH the
 * last head and the last closer, so it reads as closed and `b` rendered a live close chip
 * while the outer `[OPTIONS:` was still open — a chip built out of broken syntax, which is
 * exactly what this filter exists to refuse. Depth is what distinguishes them.
 */
import { describe, it, expect } from 'vitest'

import { findOptionMarkers, matchActionMarkers, stripActionMarkers } from '../app-sdk/protocol/optionMarker'

const A = '[OPTION-ACTIONS: close=Alpha]'
const B = '[OPTION-ACTIONS: close=Bravo]'

describe('nested action markers are counted by depth', () => {
  it('refuses a SIBLING action after a balanced nested pair in an open head', () => {
    const text = `[OPTIONS: broken ${A} ${B}`
    expect(matchActionMarkers(text)).toEqual([])
  })

  it('leaves that whole malformed run visible', () => {
    // Paired with the matcher: a span refused as a marker must not be stripped either,
    // or half the malformed line vanishes with no chip to show for it.
    const text = `[OPTIONS: broken ${A} ${B}`
    expect(stripActionMarkers(text)).toBe(text)
  })

  it('refuses a THIRD sibling too — depth does not decay to zero', () => {
    const text = `[OPTIONS: broken ${A} ${B} ${A}`
    expect(matchActionMarkers(text)).toEqual([])
  })

  it('still ACCEPTS two well-formed siblings when no head is open', () => {
    // The control that keeps the widening honest. Without it, a filter that suppressed on
    // any preceding head would pass every assertion above while killing legitimate chips.
    const text = `${A} ${B}`
    expect(matchActionMarkers(text)).toHaveLength(2)
    expect(stripActionMarkers(text).trim()).toBe('')
  })

  it('still ACCEPTS an action after a CLOSED content marker on the same line', () => {
    const text = `[OPTIONS: A | B] ${A}`
    expect(matchActionMarkers(text)).toHaveLength(1)
  })

  it('a stray closer does not cancel a genuinely open head', () => {
    // The closer pops an EMPTY stack, so it cannot offset the real head that follows it.
    const text = `] [OPTIONS: broken ${A}`
    expect(matchActionMarkers(text)).toEqual([])
  })

  it('a CITATION bracket inside an open head does not cancel it', () => {
    // The discriminating case: a bare depth counter let `[1]`'s closer cancel the open
    // `[OPTIONS:` head, so this rendered a chip the same line without a citation suppressed.
    const text = `[OPTIONS: see [1] for details ${A}`
    expect(matchActionMarkers(text)).toEqual([])
  })

  it('several citations inside an open head still do not cancel it', () => {
    const text = `[OPTIONS: see [1] and [2] here ${A}`
    expect(matchActionMarkers(text)).toEqual([])
  })

  it('control: the citation line without the open head DOES accept the action', () => {
    // Proves the two cases above are caused by the open head, not by the citation, which
    // must stay ordinary text.
    const text = `see [1] for details ${A}`
    expect(matchActionMarkers(text)).toHaveLength(1)
  })

  it('a citation AFTER a closed content marker leaves the action accepted', () => {
    const text = `[OPTIONS: A | B] see [1] ${A}`
    expect(matchActionMarkers(text)).toHaveLength(1)
  })

  it('an open head does not reach past its own newline', () => {
    const text = `[OPTIONS: broken\n${A}`
    expect(matchActionMarkers(text)).toHaveLength(1)
  })

  it('a MISMATCHED nested pair does not cancel the head', () => {
    const text = `[OPTIONS: x 【y] z] ${A}`
    expect(matchActionMarkers(text)).toEqual([])
  })

  it('leaves that mismatched-pair run visible', () => {
    const text = `[OPTIONS: x 【y] z] ${A}`
    expect(stripActionMarkers(text)).toBe(text)
  })

  it('a MATCHED nested pair still pops, so the head can close after it', () => {
    const text = `[OPTIONS: see [1] here] ${A}`
    expect(matchActionMarkers(text)).toHaveLength(1)
  })

  it('a MATCHED lookalike pair inside an open head does not cancel it either', () => {
    const text = `[OPTIONS: see 【1】 for details ${A}`
    expect(matchActionMarkers(text)).toEqual([])
  })

  it('a CONTINUATION closer is label text, not a terminator', () => {
    const text = `[OPTIONS: Alpha ] | Bravo ${A}`
    expect(matchActionMarkers(text)).toEqual([])
    expect(stripActionMarkers(text)).toBe(text)
    expect(matchActionMarkers(`[OPTIONS: Alpha ]] ${A}`)).toHaveLength(1)
  })

  it('CONTROL: a lookalike closer TERMINATING the head does close it', () => {
    // The terminator is a closer class, so this content marker parses and yields its pills;
    // popping the head frame by type instead would suppress a legitimate chip here.
    const text = `[OPTIONS: a】 ${A}`
    expect(findOptionMarkers(text)).toHaveLength(1)
    expect(matchActionMarkers(text)).toHaveLength(1)
  })

  it.each(['[OPTION-ACTIONS: dropped [OPTIONS: A | B]', '[OPTIONS: dropped [OPTIONS: A | B]'])(
    'refuses a CONTENT marker nested inside an unclosed head of EITHER kind — %s',
    src => expect(findOptionMarkers(src)).toEqual([]),
  )
})
