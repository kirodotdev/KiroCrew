// @vitest-environment happy-dom
import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { FileRenderer } from '@pierre/diffs'

vi.mock('@pierre/diffs/react', async importOriginal => ({
  ...await importOriginal<typeof import('@pierre/diffs/react')>(),
  File: ({ file }: { file: { contents: string } }) => <pre data-testid="pierre-file">{file.contents}</pre>,
}))

const { PierreCodeImpl, normalizeLoneCarriageReturns } = await import('../pierre/PierreImpl')
const { parseBlocks } = await import('../hooks/useBlockAssembler')

const THEMES = { dark: 'pierre-dark', light: 'pierre-light' } as const

describe('PierreCode lone carriage returns', () => {
  it('normalized contents render in the library', async () => {
    const contents = 'root/\r├── a.md\r\n└── b.md\n'
    const normalized = normalizeLoneCarriageReturns(contents)
    expect(normalized).toBe('root/\n├── a.md\r\n└── b.md\n')
    await expect(new FileRenderer({ theme: THEMES }).asyncRender({ name: 'tree.txt', contents: normalized, lang: 'text' }))
      .resolves.toBeDefined()
  })

  it('hands Pierre normalized contents', () => {
    const { getByTestId } = render(<PierreCodeImpl file={{ name: 'a.txt', contents: 'one\rtwo' }} />)
    expect(getByTestId('pierre-file').textContent).toBe('one\ntwo')
  })

  it('a fence whose last line ends in a lone \\r closes there with no trailing break', () => {
    const [, code, after] = parseBlocks('a\n\n```text\nx\ry\r```\n\nz\n', false)
    expect(code).toMatchObject({ type: 'code', content: 'x\ny', complete: true })
    expect(after.type).toBe('markdown')
    expect(after.content.trim()).toBe('z')
  })

  it('a CRLF fence keeps no \\r on its lines', () => {
    expect(parseBlocks('```text\r\nx\r\ny\r\n```\r\n', false)[0]).toMatchObject({ type: 'code', content: 'x\ny' })
  })
})
