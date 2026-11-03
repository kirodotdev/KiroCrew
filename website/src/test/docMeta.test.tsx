/**
 * A doc's page header: icon and cover kept in the file's front matter, and the
 * header that shows and changes them.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import DocHeader from '../pages/chat/page/DocHeader'
import { coverBackground, joinDoc, randomGradient, splitDoc } from '../pages/chat/page/docMeta'

describe('doc front matter', () => {
  it('splits icon and cover off the body and writes them back, keeping other lines', () => {
    const file = '---\nicon: 🎯\ncover: gradient:2\ntags: work\n---\n# Plan\n\ntext'
    const doc = splitDoc(file)
    expect(doc.meta).toEqual({ icon: '🎯', cover: 'gradient:2' })
    expect(doc.other).toEqual(['tags: work'])
    expect(doc.body).toBe('# Plan\n\ntext')
    expect(joinDoc(doc)).toBe(file)
  })

  it('a doc without front matter is all body, and clearing both removes the block', () => {
    expect(splitDoc('# Plan')).toEqual({ meta: {}, other: [], body: '# Plan' })
    const doc = splitDoc('---\nicon: 🎯\n---\nbody')
    expect(joinDoc({ ...doc, meta: {} })).toBe('body')
    // A rule written at the top of a body is not front matter without a closing line.
    expect(splitDoc('---\nnot closed').body).toBe('---\nnot closed')
  })

  it('draws gradients, colours and https images only', () => {
    expect(coverBackground('gradient:0')).toContain('linear-gradient')
    expect(coverBackground('color:1')).toContain('color-mix')
    expect(coverBackground('https://example.com/a.png')).toBe('center / cover no-repeat url("https://example.com/a.png")')
    expect(coverBackground('http://example.com/a.png')).toBeNull()
    expect(coverBackground('https://x.com/a.png") ; background: red')).toBeNull()
    expect(coverBackground('gradient:99')).toBeNull()
    expect(randomGradient(() => 0)).toBe('gradient:0')
  })
})

describe('doc header', () => {
  it('offers Add icon and Add cover when the page has neither, and adds them', () => {
    const onChange = vi.fn()
    render(<DocHeader meta={{}} onChange={onChange} />)
    expect(screen.queryByTestId('doc-cover')).toBeNull()
    fireEvent.click(screen.getByTestId('doc-add-cover'))
    expect(onChange.mock.calls[0][0].cover).toMatch(/^gradient:\d+$/)
    fireEvent.click(screen.getByTestId('doc-add-icon'))
    expect(onChange.mock.calls[1][0].icon).toBeTruthy()
  })

  it('shows the cover and icon; the pickers change and remove them', () => {
    const onChange = vi.fn()
    render(<DocHeader meta={{ icon: '🎯', cover: 'gradient:1' }} onChange={onChange} />)
    expect(screen.getByTestId('doc-cover')).toBeTruthy()
    expect(screen.queryByTestId('doc-add-cover')).toBeNull()
    fireEvent.click(screen.getByTestId('doc-icon'))
    fireEvent.click(screen.getByLabelText('Icon 🚀'))
    expect(onChange).toHaveBeenLastCalledWith({ icon: '🚀', cover: 'gradient:1' })
    fireEvent.click(screen.getByTestId('doc-cover-change'))
    fireEvent.click(screen.getByLabelText('Colour 2'))
    expect(onChange).toHaveBeenLastCalledWith({ icon: '🎯', cover: 'color:1' })
    fireEvent.click(screen.getByTestId('doc-cover-change'))
    fireEvent.click(screen.getByTestId('doc-cover-remove'))
    expect(onChange).toHaveBeenLastCalledWith({ icon: '🎯', cover: undefined })
  })
})
