/**
 * The chat scratch editor's `cacheKey` addresses Pierre's highlight cache, so
 * two snippets that share a key serve each other's cached tokens. Language +
 * character count is not enough to tell snippets apart — a chat message full
 * of same-language one-liners collides constantly — so the key carries a
 * content hash. These tests pin the three properties that key needs:
 * content-distinguishing, stable for identical content, and bounded in size.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

const hoisted = vi.hoisted(() => ({ keys: [] as string[] }))

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreEditor: ({ file }: { file: { cacheKey?: string } }) => {
    hoisted.keys.push(file.cacheKey ?? '')
    return <div data-testid="pierre-editor" />
  },
}))

// The rendered (non-editing) block only matters here as the host of the pencil
// that swaps in the editor.
vi.mock('../components/CodeBlock', () => ({
  CodeBlock: ({ headerActions }: { headerActions?: React.ReactNode }) => <div>{headerActions}</div>,
}))

import EditableCodeBlock from '../components/EditableCodeBlock'

/** Mounts the block, clicks the pencil, and returns the key the editor got. */
function cacheKeyFor(code: string, lang: string): string {
  hoisted.keys.length = 0
  render(<EditableCodeBlock code={code} lang={lang} complete />)
  fireEvent.click(screen.getAllByRole('button')[0])
  expect(screen.getByTestId('pierre-editor')).toBeTruthy()
  const key = hoisted.keys[hoisted.keys.length - 1]
  cleanup()
  return key
}

beforeEach(() => { hoisted.keys.length = 0 })

describe('EditableCodeBlock editor cache key', () => {
  it('distinguishes same-language snippets of equal length', () => {
    const a = 'const a = 1'
    const b = 'const b = 2'
    expect(a.length).toBe(b.length)
    expect(cacheKeyFor(a, 'ts')).not.toBe(cacheKeyFor(b, 'ts'))
  })

  it('reuses one key for identical content, which is what the cache is for', () => {
    expect(cacheKeyFor('print("hi")', 'python')).toBe(cacheKeyFor('print("hi")', 'python'))
  })

  it('keeps the key bounded instead of embedding the source', () => {
    const long = 'x'.repeat(20_000)
    const key = cacheKeyFor(long, 'ts')
    expect(key).not.toContain(long)
    expect(key.length).toBeLessThan(80)
  })
})
