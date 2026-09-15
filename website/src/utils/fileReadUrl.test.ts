import { describe, it, expect } from 'vitest'
import {
  fileReadUrl,
  fileDownloadUrl,
  fileStreamUrl,
  fileOfficePreviewUrl,
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
})
