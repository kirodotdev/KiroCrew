import { describe, it, expect } from 'vitest'
import React from 'react'
import { render } from '@testing-library/react'
import { escapeRegex, highlightText } from '../utils/highlightText'

describe('escapeRegex', () => {
  it('escapes all regex special characters', () => {
    expect(escapeRegex('.*+?^${}()|[]\\')).toBe('\\.\\*\\+\\?\\^\\$\\{\\}\\(\\)\\|\\[\\]\\\\')
  })
  it('returns empty string unchanged', () => {
    expect(escapeRegex('')).toBe('')
  })
  it('returns plain text unchanged', () => {
    expect(escapeRegex('hello world')).toBe('hello world')
  })
})

describe('highlightText', () => {
  it('returns plain text when term is empty', () => {
    expect(highlightText('hello', '', false, -1)).toBe('hello')
  })

  it('returns plain text when term has no match', () => {
    expect(highlightText('hello', 'xyz', false, -1)).toBe('hello')
  })

  it('wraps single match in <mark> with search-match class', () => {
    const { container } = render(<>{highlightText('hello world', 'world', false, -1)}</>)
    const marks = container.querySelectorAll('mark')
    expect(marks).toHaveLength(1)
    expect(marks[0].textContent).toBe('world')
    expect(marks[0].className).toBe('search-match')
  })

  it('wraps multiple matches in <mark> elements', () => {
    const { container } = render(<>{highlightText('foo bar foo', 'foo', false, -1)}</>)
    const marks = container.querySelectorAll('mark')
    expect(marks).toHaveLength(2)
    expect(marks[0].textContent).toBe('foo')
    expect(marks[1].textContent).toBe('foo')
  })

  it('case-insensitive by default', () => {
    const { container } = render(<>{highlightText('Hello HELLO', 'hello', false, -1)}</>)
    expect(container.querySelectorAll('mark')).toHaveLength(2)
  })

  it('case-sensitive when caseSensitive=true', () => {
    const { container } = render(<>{highlightText('Hello HELLO hello', 'hello', true, -1)}</>)
    expect(container.querySelectorAll('mark')).toHaveLength(1)
    expect(container.querySelector('mark')!.textContent).toBe('hello')
  })

  it('uses search-current class only for the specified occurrence', () => {
    const { container } = render(<>{highlightText('a a a', 'a', false, 1)}</>)
    const marks = container.querySelectorAll('mark')
    expect(marks).toHaveLength(3)
    expect(marks[0].className).toBe('search-match')
    expect(marks[1].className).toBe('search-current')
    expect(marks[2].className).toBe('search-match')
  })

  it('uses search-match for all when currentOcc=-1', () => {
    const { container } = render(<>{highlightText('a a', 'a', false, -1)}</>)
    const marks = container.querySelectorAll('mark')
    expect(marks).toHaveLength(2)
    expect(marks[0].className).toBe('search-match')
    expect(marks[1].className).toBe('search-match')
  })

  it('uses search-current for first occurrence when currentOcc=0', () => {
    const { container } = render(<>{highlightText('hello world hello', 'hello', false, 0)}</>)
    const marks = container.querySelectorAll('mark')
    expect(marks[0].className).toBe('search-current')
    expect(marks[1].className).toBe('search-match')
  })

  it('handles term at start of string', () => {
    const { container } = render(<>{highlightText('abc def', 'abc', false, -1)}</>)
    expect(container.querySelector('mark')!.textContent).toBe('abc')
  })

  it('handles term at end of string', () => {
    const { container } = render(<>{highlightText('abc def', 'def', false, -1)}</>)
    expect(container.querySelector('mark')!.textContent).toBe('def')
  })

  it('handles consecutive matches', () => {
    const { container } = render(<>{highlightText('aaa', 'a', false, -1)}</>)
    expect(container.querySelectorAll('mark')).toHaveLength(3)
  })

  it('handles special regex characters in search term', () => {
    const { container } = render(<>{highlightText('foo.bar a+b', 'foo.bar', false, -1)}</>)
    expect(container.querySelectorAll('mark')).toHaveLength(1)
    expect(container.querySelector('mark')!.textContent).toBe('foo.bar')
  })

  it('preserves non-matching text between matches', () => {
    const { container } = render(<>{highlightText('a X b X c', 'X', false, -1)}</>)
    expect(container.textContent).toBe('a X b X c')
    expect(container.querySelectorAll('mark')).toHaveLength(2)
  })

  it('returns original string reference when no match', () => {
    const text = 'hello world'
    const result = highlightText(text, 'xyz', false, -1)
    expect(result).toBe(text)
  })

  it('currentOcc beyond last occurrence produces all search-match', () => {
    const { container } = render(<>{highlightText('a b a', 'a', false, 99)}</>)
    const marks = container.querySelectorAll('mark')
    expect(marks).toHaveLength(2)
    expect(marks[0].className).toBe('search-match')
    expect(marks[1].className).toBe('search-match')
  })
})
