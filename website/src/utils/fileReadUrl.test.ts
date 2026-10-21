import { describe, it, expect } from 'vitest'
import {
  fileReadUrl,
  fileDownloadUrl,
  fileStreamUrl,
  fileOfficePreviewUrl,
  fileOfficeSlidesUrl,
  fileOfficeSlideUrl,
  partialDownloadName,
} from './fileReadUrl'

// The four builders carry the client half of the contract that a path holding
// URL-reserved characters reaches the file endpoints intact: the value is
// percent-encoded into the query string, so '#' cannot truncate the path at a
// fragment and '&' cannot split it into a second parameter.
//
// Note what encodeURIComponent does NOT touch: '(', ')', "'", '!', '*' and '~'
// are unreserved to it and travel literally. That is correct -- they carry no
// meaning inside a query value -- but it means no amount of client-side encoding
// can hide them from the server. A folder named by the "Name (alias).md"
// convention therefore only works if the server accepts a literal paren. These
// cases pin both halves of that split so neither side is "fixed" alone.
const RESERVED = "/home/u/One on one/Ada Lovelace (ada) #2 & 50%.md"

const builders: Array<[string, (p: string) => string, string]> = [
  ['fileReadUrl', fileReadUrl, '/api/file-read'],
  ['fileDownloadUrl', fileDownloadUrl, '/api/file-download'],
  ['fileStreamUrl', fileStreamUrl, '/api/file-stream'],
  ['fileOfficePreviewUrl', fileOfficePreviewUrl, '/api/file-office-preview'],
  ['fileOfficeSlidesUrl', fileOfficeSlidesUrl, '/api/file-office-slides'],
]

describe('file endpoint URL builders', () => {
  for (const [name, build, endpoint] of builders) {
    it(`${name} percent-encodes the reserved characters in a path`, () => {
      const url = build(RESERVED)
      expect(url).toBe(`${endpoint}?path=${encodeURIComponent(RESERVED)}`)
      // The characters that would otherwise change how the URL parses.
      expect(url).toContain('%23') // '#' — would truncate at a fragment
      expect(url).toContain('%26') // '&' — would start a second parameter
      expect(url).toContain('%25') // '%' — would open a stray escape
      expect(url).toContain('%20') // ' '
      expect(url).toContain('%2F') // '/' — segment separators, not path structure
    })

    it(`${name} leaves parentheses literal, so the server must accept them`, () => {
      expect(build(RESERVED)).toContain('(ada)')
    })

    it(`${name} round-trips the path back byte-for-byte`, () => {
      const value = new URL(build(RESERVED), 'http://localhost').searchParams.get('path')
      expect(value).toBe(RESERVED)
    })
  }

  it('appends resolve=1 only for a relative path, after the encoded value', () => {
    expect(fileReadUrl('notes/One on one (2026).md')).toBe(
      '/api/file-read?path=' + encodeURIComponent('notes/One on one (2026).md') + '&resolve=1',
    )
    expect(fileReadUrl(RESERVED)).not.toContain('resolve=1')
  })

  it('fileOfficeSlideUrl appends the slide number after the encoded path and resolve flag', () => {
    const url = fileOfficeSlideUrl('decks/Q3 (draft) #2.pptx', 7)
    expect(url).toBe(
      '/api/file-office-slide?path=' + encodeURIComponent('decks/Q3 (draft) #2.pptx') + '&resolve=1&n=7',
    )
    const params = new URL(fileOfficeSlideUrl(RESERVED, 12), 'http://localhost').searchParams
    expect(params.get('path')).toBe(RESERVED)
    expect(params.get('n')).toBe('12')
    expect(params.get('resolve')).toBeNull()
    expect(params.get('digest')).toBeNull()
    const pinned = new URL(fileOfficeSlideUrl(RESERVED, 12, 'f'.repeat(64)), 'http://localhost').searchParams
    expect(pinned.get('digest')).toBe('f'.repeat(64))
    expect(pinned.get('n')).toBe('12')
  })
})

describe('partialDownloadName', () => {
  it('inserts .partial before the extension so the OS keeps the file type', () => {
    expect(partialDownloadName('/home/u/release-checklist.md')).toBe('release-checklist.partial.md')
    expect(partialDownloadName('b.md')).toBe('b.partial.md')
    expect(partialDownloadName('/a/b/data.json')).toBe('data.partial.json')
  })

  it('inserts before the LAST extension of a multi-dot name', () => {
    expect(partialDownloadName('archive.tar.gz')).toBe('archive.tar.partial.gz')
  })

  it('appends when there is no extension to preserve', () => {
    expect(partialDownloadName('Makefile')).toBe('Makefile.partial')
    expect(partialDownloadName('/etc/hosts')).toBe('hosts.partial')
  })

  it('treats a leading dot as a dotfile marker, not an extension', () => {
    // `.env` is the whole name, not a stem of `` with extension `env`.
    expect(partialDownloadName('.env')).toBe('.env.partial')
    expect(partialDownloadName('/home/u/.bashrc')).toBe('.bashrc.partial')
  })

  it('appends for a trailing-dot name rather than splitting on the empty extension', () => {
    expect(partialDownloadName('weird.')).toBe('weird..partial')
  })
})
