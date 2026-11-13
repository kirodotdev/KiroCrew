import { describe, expect, it } from 'vitest'
import { readIndexCss } from './indexCssSource'

const INDEX_CSS = readIndexCss()

// jsdom evaluates no media queries and computes no layout, so this reads the
// stylesheet's text. What it guards: on a touch screen the composer's typed text
// and its placeholder are one size. Its auto-size measures the placeholder at the
// FIELD's font size (chat-input/sizing.ts), so a coarse-pointer rule that raises
// the field (or shrinks the hint) makes an empty composer measure a line taller
// than the hint it shows. Focus zoom is held off by the viewport's
// `maximum-scale=1` instead, which noPageZoom.test.ts pins.
describe('mobile composer typography', () => {
  it('sets no coarse-pointer font size on the composer typography hook', () => {
    const coarseBlocks = INDEX_CSS.match(/@media\s*\(pointer:\s*coarse\)\s*\{[\s\S]*?\n\}/g) ?? []
    const sized = coarseBlocks.filter(b => /\[data-composer-typo\][^{]*\{[^}]*font-size/.test(b))
    expect(sized, 'a coarse-pointer rule sizes [data-composer-typo] again -- the empty composer will measure its hint at the wrong size').toEqual([])
  })
})
