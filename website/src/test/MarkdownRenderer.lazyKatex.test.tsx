import { describe, it, expect } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { isKatexChunkError, loadKatex, mayHoldMath } from '../components/markdown/lazyKatex'

// No beforeAll(loadKatex) here: the first render must see KaTeX not loaded yet,
// so only one test renders math (the order may be shuffled).
describe('KaTeX loads on demand', () => {
  it('names no chunk error as its own while no load is in flight', () => {
    expect(isKatexChunkError(new Error('Failed to fetch dynamically imported module: http://x/assets/katexPlugin-Ab1.js'))).toBe(false)
  })

  it('shows TeX source first, then KaTeX once its chunk lands', async () => {
    const { container } = render(<MarkdownRenderer content={'before\n\n$$x^2$$\n\nafter'} />)
    // A math fence that stays in the markdown (tilde, or inside a quote) re-parses too.
    // So does raw HTML math, which the sanitizer keeps.
    const fence = render(<MarkdownRenderer content={'~~~math\na+b\n~~~\n\n> ```math\n> c+d\n> ```\n\nraw <code class="math-inline">e+f</code>'} />)
    expect(fence.container.querySelector('.katex')).toBeNull()
    expect(container.querySelector('.katex')).toBeNull()
    expect(container.textContent).toContain('x^2')
    // The render above started the load; only KaTeX chunk errors are its own.
    expect(isKatexChunkError(new Error('Failed to fetch dynamically imported module: http://x/assets/katexPlugin-Ab1.js'))).toBe(true)
    expect(isKatexChunkError(new Error('Failed to fetch dynamically imported module: http://x/assets/katex-Cd2.js'))).toBe(true)
    expect(isKatexChunkError(new Error('Failed to fetch dynamically imported module: http://x/assets/AppPage-Zz9.js'))).toBe(false)
    await waitFor(() => expect(container.querySelector('.katex')).not.toBeNull(), { timeout: 10000 })
    expect(container.textContent).toContain('before')
    expect(container.textContent).toContain('after')
    await waitFor(() => expect(fence.container.querySelectorAll('.katex').length).toBe(3))
    // A later math block renders at once.
    await loadKatex()
    const later = render(<MarkdownRenderer content={'and \\(y\\)'} />)
    expect(later.container.querySelectorAll('.katex').length).toBe(1)
  })

  it('re-parses only text that may hold math', () => {
    expect(mayHoldMath('$$x$$')).toBe(true)
    expect(mayHoldMath('see \\(x\\)')).toBe(true)
    expect(mayHoldMath('\\[x\\]')).toBe(true)
    expect(mayHoldMath('~~~math\nx\n~~~')).toBe(true)
    expect(mayHoldMath('<code class="math-inline">x</code>')).toBe(true)
    expect(mayHoldMath('```ts\nconst n = 1\n```')).toBe(false)
    expect(mayHoldMath('costs $5 and $6')).toBe(false)
    expect(mayHoldMath('plain prose')).toBe(false)
  })
})
