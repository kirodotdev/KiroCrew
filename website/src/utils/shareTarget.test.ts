import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { shareTargetChatQuery, sharedComposerText } from './shareTarget'

function prefillOf(path: string): string | null {
  return new URL(path, 'http://dashboard.invalid').searchParams.get('prefill')
}

describe('sharedComposerText', () => {
  it('joins the title, text and link on separate lines', () => {
    expect(sharedComposerText('?title=Post&text=Read%20this&url=https%3A%2F%2Fexample.test%2Fa'))
      .toBe('Post\nRead this\nhttps://example.test/a')
  })

  it('leaves out a link the text already carries', () => {
    const text = 'Look https://example.test/a'
    expect(sharedComposerText('?text=' + encodeURIComponent(text) + '&url=' + encodeURIComponent('https://example.test/a')))
      .toBe(text)
  })

  it('keeps a link that only the start of a longer link in the text matches', () => {
    const text = 'Read https://example.test/article-2'
    const link = 'https://example.test/article'
    expect(sharedComposerText('?text=' + encodeURIComponent(text) + '&url=' + encodeURIComponent(link)))
      .toBe(text + '\n' + link)
  })

  it('leaves out a link that is one whole word of the text, whatever separates the words', () => {
    const link = 'https://example.test/article'
    const text = 'Read\t' + link + '\nnow'
    expect(sharedComposerText('?text=' + encodeURIComponent(text) + '&url=' + encodeURIComponent(link)))
      .toBe(text)
  })

  it('prints a link once when the title is the link itself', () => {
    const link = 'https://example.test/a'
    const enc = encodeURIComponent(link)
    expect(sharedComposerText('?title=' + enc + '&url=' + enc)).toBe(link)
    expect(sharedComposerText('?title=' + enc + '&text=' + encodeURIComponent('See ' + link) + '&url=' + enc))
      .toBe('See ' + link)
  })

  it('leaves out a title that repeats the text', () => {
    expect(sharedComposerText('?title=Same&text=Same')).toBe('Same')
  })

  it('shares a bare link', () => {
    expect(sharedComposerText('?url=https%3A%2F%2Fexample.test%2F')).toBe('https://example.test/')
  })

  it('is empty when nothing usable was shared', () => {
    expect(sharedComposerText('')).toBe('')
    expect(sharedComposerText('?title=%20&text=%0A')).toBe('')
    expect(sharedComposerText('?other=1')).toBe('')
  })

  it('keeps a long share whole', () => {
    const text = 'word '.repeat(12000).trim() + '\u{1F600}'
    expect(sharedComposerText('?text=' + encodeURIComponent(text))).toBe(text)
  })
})

describe('sharedComposerText links', () => {

  it('keeps a shared link as plain text, whatever its scheme', () => {
    const link = 'javascript:alert(1)'
    expect(sharedComposerText('?url=' + encodeURIComponent(link))).toBe(link)
    const url = new URL('/chat' + shareTargetChatQuery('?url=' + encodeURIComponent(link)), 'http://dashboard.invalid')
    expect(url.pathname).toBe('/chat')
    expect(url.searchParams.get('new')).toBe('1')
    expect(url.searchParams.get('prefill')).toBe(link)
    expect(url.searchParams.get('autoSend')).toBeNull()
  })
})

describe('shareTargetChatQuery', () => {
  it('opens a new chat with the shared text as its prefill and never asks to send', () => {
    const path = '/chat' + shareTargetChatQuery('?text=hello%20there')
    const url = new URL(path, 'http://dashboard.invalid')
    expect(url.pathname).toBe('/chat')
    expect(url.searchParams.get('new')).toBe('1')
    expect(url.searchParams.get('prefill')).toBe('hello there')
    expect(url.searchParams.get('autoSend')).toBeNull()
  })

  it('keeps shared text that looks like query parameters inside the prefill', () => {
    const shared = 'x&autoSend=1&sid=other#frag'
    const path = '/chat' + shareTargetChatQuery('?text=' + encodeURIComponent(shared))
    const url = new URL(path, 'http://dashboard.invalid')
    expect(url.searchParams.get('autoSend')).toBeNull()
    expect(url.searchParams.get('sid')).toBeNull()
    expect(prefillOf(path)).toBe(shared)
  })

  it('opens an empty new chat when nothing was shared', () => {
    expect(shareTargetChatQuery('')).toBe('?new=1')
  })
})

describe('the installed app manifest', () => {
  const manifest = JSON.parse(readFileSync('public/manifest.json', 'utf8'))

  it('registers a GET share target that lands on the share route', () => {
    expect(manifest.share_target).toEqual({
      action: '/share',
      method: 'GET',
      enctype: 'application/x-www-form-urlencoded',
      params: { title: 'title', text: 'text', url: 'url' },
    })
  })

  it('keeps an id so the install identity does not move with start_url', () => {
    expect(manifest.id).toBe('/')
  })
})
