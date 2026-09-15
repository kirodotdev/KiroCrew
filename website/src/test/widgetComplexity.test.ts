import { describe, it, expect } from 'vitest'
import {
  analyzeWidgetComplexity,
  TAILWIND_COMPLEXITY_THRESHOLD,
} from '../lib/widgetComplexity'

/** Build HTML carrying `n` distinct Tailwind utility tokens. */
function withUniqueClasses(n: number): string {
  const tokens: string[] = []
  for (let i = 0; i < n; i++) tokens.push(`p-${i}`)
  return `<div class="${tokens.join(' ')}">x</div>`
}

describe('analyzeWidgetComplexity', () => {
  it('reports no indicator for a widget with no Tailwind classes', () => {
    const r = analyzeWidgetComplexity('<div style="color:red">Hello</div>')
    expect(r.tailwindClassCount).toBe(0)
    expect(r.needsProgressIndicator).toBe(false)
  })

  it('reports no indicator for a handful of Tailwind classes', () => {
    const r = analyzeWidgetComplexity(
      '<div class="p-4 flex items-center bg-blue-500"><span class="text-white font-bold">Hi</span></div>',
    )
    expect(r.tailwindClassCount).toBeLessThan(TAILWIND_COMPLEXITY_THRESHOLD)
    expect(r.needsProgressIndicator).toBe(false)
  })

  it('requires the indicator above the class threshold', () => {
    const r = analyzeWidgetComplexity(withUniqueClasses(TAILWIND_COMPLEXITY_THRESHOLD + 5))
    expect(r.tailwindClassCount).toBeGreaterThan(TAILWIND_COMPLEXITY_THRESHOLD)
    expect(r.needsProgressIndicator).toBe(true)
  })

  it('is exclusive at the threshold boundary', () => {
    // Exactly at the threshold is still fast enough; one more crosses it.
    expect(analyzeWidgetComplexity(withUniqueClasses(TAILWIND_COMPLEXITY_THRESHOLD))
      .needsProgressIndicator).toBe(false)
    expect(analyzeWidgetComplexity(withUniqueClasses(TAILWIND_COMPLEXITY_THRESHOLD + 1))
      .needsProgressIndicator).toBe(true)
  })

  it('a <style> block does not change the verdict', () => {
    // The widget's own CSS cannot style Tailwind utilities, so it neither
    // shortens nor lengthens the compile the indicator is covering.
    const many = withUniqueClasses(TAILWIND_COMPLEXITY_THRESHOLD + 5)
    const withStyle = `<style>.a{color:red}</style>${many}`
    expect(analyzeWidgetComplexity(withStyle).needsProgressIndicator).toBe(true)
  })

  it('element count alone never triggers the indicator', () => {
    // Many elements sharing few classes compile fast — size is not the signal.
    const rows = Array.from({ length: 300 }, (_, i) => `<div class="p-1 flex">r${i}</div>`)
    const r = analyzeWidgetComplexity(rows.join(''))
    expect(r.tailwindClassCount).toBe(2)
    expect(r.needsProgressIndicator).toBe(false)
  })

  it('counts arbitrary-value classes', () => {
    const r = analyzeWidgetComplexity('<div class="bg-[#1a1a2e] text-[14px] w-[200px]">x</div>')
    expect(r.tailwindClassCount).toBe(3)
  })

  it('ignores author-defined classes', () => {
    const r = analyzeWidgetComplexity('<div class="widget-root app-container panel-header">x</div>')
    expect(r.tailwindClassCount).toBe(0)
  })

  it('handles the className spelling', () => {
    expect(analyzeWidgetComplexity('<div className="flex p-4 bg-card">x</div>')
      .tailwindClassCount).toBe(3)
  })

  it('counts each unique class once', () => {
    const r = analyzeWidgetComplexity(
      '<div class="p-4 flex">A</div><div class="p-4 flex">B</div><div class="p-4 flex">C</div>',
    )
    expect(r.tailwindClassCount).toBe(2)
  })

  it('counts variant-prefixed classes separately', () => {
    const r = analyzeWidgetComplexity('<div class="sm:flex md:grid lg:hidden xl:block">x</div>')
    expect(r.tailwindClassCount).toBe(4)
  })

  it('does not expose a skipTailwind inference', () => {
    // Regression guard. An earlier revision inferred "skip the Tailwind runtime"
    // from this static scan. That is unsound: the scan cannot see classes a
    // widget's own <script> adds at runtime, so a dynamically-built widget would
    // render completely unstyled. This module stays a pure complexity signal.
    const r = analyzeWidgetComplexity('<style>.a{color:red}</style><div class="a">Hi</div>')
    expect('skipTailwind' in r).toBe(false)
  })
})
