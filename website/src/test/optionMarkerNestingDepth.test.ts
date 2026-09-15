/**
 * A marker that sits inside a still-open head is not a marker, counted by bracket DEPTH.
 *
 * The pairwise `lastCloser < lastHead` test could not see this: in
 * `[OPTIONS: x [OPTIONS: a] [OPTIONS: b]` the first pair supplies BOTH the last head and the
 * last closer, so it reads as closed and `b` offered pills while the outer `[OPTIONS:` was
 * still open. Depth is what distinguishes them.
 */
import { describe, it, expect } from 'vitest'

import { findOptionMarkers, stripOptionMarkers } from '../app-sdk/protocol/optionMarker'

const A = '[OPTIONS: Alpha]'
const B = '[OPTIONS: Bravo]'

describe('nested markers are counted by depth', () => {
  it('refuses a SIBLING after a balanced nested pair in an open head', () => {
    const text = `[OPTIONS: broken ${A} ${B}`
    expect(findOptionMarkers(text)).toEqual([])
  })

  it('leaves that whole malformed run visible', () => {
    // A span refused as a marker must not be stripped either, or half the line vanishes.
    const text = `[OPTIONS: broken ${A} ${B}`
    expect(stripOptionMarkers(text)).toBe(text)
  })

  it('refuses a THIRD sibling too — depth does not decay to zero', () => {
    expect(findOptionMarkers(`[OPTIONS: broken ${A} ${B} ${A}`)).toEqual([])
  })

  it('still ACCEPTS two well-formed siblings when no head is open', () => {
    // The control: a filter that suppressed on any preceding head would pass the cases above.
    const text = `${A} ${B}`
    expect(findOptionMarkers(text)).toHaveLength(2)
    expect(stripOptionMarkers(text).trim()).toBe('')
  })

  it('a stray closer does not cancel a genuinely open head', () => {
    expect(findOptionMarkers(`] [OPTIONS: broken ${A}`)).toEqual([])
  })

  it('a CITATION bracket inside an open head does not cancel it', () => {
    // A bare depth counter let `[1]`'s closer cancel the open head.
    expect(findOptionMarkers(`[OPTIONS: see [1] for details ${A}`)).toEqual([])
  })

  it('several citations inside an open head still do not cancel it', () => {
    expect(findOptionMarkers(`[OPTIONS: see [1] and [2] here ${A}`)).toEqual([])
  })

  it('control: the citation line without the open head DOES accept the marker', () => {
    expect(findOptionMarkers(`see [1] for details ${A}`)).toHaveLength(1)
  })

  it('an open head does not reach past its own newline', () => {
    expect(findOptionMarkers(`[OPTIONS: broken\n${A}`)).toHaveLength(1)
  })

  it('a MISMATCHED nested pair does not cancel the head, and the run stays visible', () => {
    const text = `[OPTIONS: x 【y] z ${A}`
    expect(findOptionMarkers(text)).toEqual([])
    expect(stripOptionMarkers(text)).toBe(text)
  })

  it('a MATCHED lookalike pair inside an open head does not cancel it either', () => {
    expect(findOptionMarkers(`[OPTIONS: see 【1】 for details ${A}`)).toEqual([])
  })

  it('a CONTINUATION closer is label text, not a terminator', () => {
    const text = `[OPTIONS: Alpha ] | Bravo ${A}`
    expect(findOptionMarkers(text)).toEqual([])
    expect(stripOptionMarkers(text)).toBe(text)
  })

  it('refuses a marker nested inside an unclosed head', () => {
    expect(findOptionMarkers('[OPTIONS: dropped [OPTIONS: A | B]')).toEqual([])
  })
})
