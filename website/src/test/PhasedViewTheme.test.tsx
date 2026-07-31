// PhasedView theming. The board used to reference three CSS custom properties
// that are not defined in ANY of index.css's theme blocks
// (--bg-secondary, --bg-tertiary, --text-muted), each with a dark-navy literal
// fallback. The fallback therefore always won, and the columns and cards stayed
// dark regardless of the active theme — visible as a navy board on a light page.
//
// Two layers of guard here:
//   1. behavioural — the rendered surfaces use tokens that exist, and carry no
//      hex literal;
//   2. structural — every custom property these two views reference is actually
//      defined in index.css, which is the check that would have caught the
//      original bug and catches the next one of its kind.
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import PhasedView from '../pages/aidlc/PhasedView'
import type { TaskDetail } from '../types'

const task = (index: number, title: string, status: string): TaskDetail => ({
  index, title, description: '', status, error: '', result: '', attempts: 1,
  depends_on: [], requires_approval: false,
})

const TASKS: TaskDetail[] = [
  task(1, 'Read the middleware', 'passed'),
  task(2, 'Swap validation', 'in_progress'),
  task(3, 'Migrate sessions', 'pending'),
  { ...task(4, 'Broken step', 'failed'), error: 'compile error' },
  task(5, 'Skipped step', 'skipped'),
]

const SRC = join(__dirname, '..')

function sourceOf(rel: string): string {
  return readFileSync(join(SRC, rel), 'utf-8')
}

/** Custom properties defined by at least one theme block in index.css. */
function definedTokens(): Set<string> {
  const css = sourceOf('index.css')
  return new Set(Array.from(css.matchAll(/(--[a-z0-9-]+)\s*:/g), (m) => m[1]))
}

/** Custom properties a source file reads via var(). */
function referencedTokens(src: string): string[] {
  return Array.from(new Set(Array.from(src.matchAll(/var\(\s*(--[a-z0-9-]+)/g), (m) => m[1])))
}

describe('PhasedView theming', () => {
  it('draws columns and cards from tokens that exist, with no hex literal', () => {
    const { container } = render(<PhasedView tasks={TASKS} onTaskClick={() => {}} />)
    const styled = Array.from(container.querySelectorAll<HTMLElement>('[style]'))
    expect(styled.length).toBeGreaterThan(0)

    const columns = styled.filter((el) => el.style.background.includes('--bg-elevated'))
    expect(columns.length).toBeGreaterThan(0)
    const cards = styled.filter((el) => el.style.background.includes('--bg-hover'))
    expect(cards.length).toBeGreaterThan(0)

    // No element may bake in a colour: a literal survives a theme switch and is
    // exactly how the original bug presented.
    for (const el of styled) {
      expect(el.getAttribute('style')).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    }
  })

  it('tints the failed group from --danger rather than a raw rgb triple', () => {
    const { container } = render(<PhasedView tasks={TASKS} onTaskClick={() => {}} />)
    const tinted = Array.from(container.querySelectorAll<HTMLElement>('[style]'))
      .filter((el) => el.getAttribute('style')?.includes('--danger'))
    expect(tinted.length).toBeGreaterThan(0)
    // The old form was rgba(239,68,68,0.08) — a fixed red with no theme input.
    const html = container.innerHTML
    expect(html).not.toMatch(/rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+/)
  })

  it('marks unsaved edits with the warn token', () => {
    const { container } = render(
      <PhasedView tasks={TASKS} onTaskClick={() => {}} pendingEditIndexes={new Set([2])} />,
    )
    const dots = Array.from(container.querySelectorAll<HTMLElement>('[style]'))
      .filter((el) => el.style.background.includes('--warn'))
    expect(dots).toHaveLength(1)
  })
})

describe('aidlc views reference only defined theme tokens', () => {
  const defined = definedTokens()

  // Confidence check on the extractor itself: if this set came back empty or
  // missing a token everything uses, the assertions below would pass vacuously.
  it('finds the real token set in index.css', () => {
    expect(defined.has('--bg-elevated')).toBe(true)
    expect(defined.has('--muted')).toBe(true)
    expect(defined.size).toBeGreaterThan(20)
  })

  it.each([
    ['pages/aidlc/PhasedView.tsx'],
    ['pages/aidlc/DagView.tsx'],
  ])('%s reads no undefined custom property', (rel) => {
    const missing = referencedTokens(sourceOf(rel)).filter((t) => !defined.has(t))
    expect(missing).toEqual([])
  })
})
