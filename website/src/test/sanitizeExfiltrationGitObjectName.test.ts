/** The browser mirror of the exfil blob signal keeps a bare git object name,
 *  in step with the backend `_exfil_blob_hit`
 *  (test/test_exfil_git_object_name.py pins the Python side).
 *
 *  A commit permalink with the SHA in its QUERY rendered as a placeholder:
 *  40 hex digits are 40 characters of the base64 class. The carve-out is the
 *  exact SHA-1 length (40), lowercase, judged on the maximal run, so every
 *  neighbouring shape still redacts. 64 hex (SHA-256) stays redacted: it is
 *  the shape of the product's own `token_hex(32)` secrets. URLs are built from parts on purpose.
 */
import { describe, it, expect } from 'vitest'
import { sanitizeExfiltrationUrls } from '../utils/sanitize'

const SHA1 = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
const SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'

function url(query: string): string {
  return 'https://git.example.com/pkg/history?' + query
}

function isRedacted(u: string): boolean {
  return !sanitizeExfiltrationUrls(`see ${u} for details`).includes(u)
}

describe('sanitizeExfiltrationUrls: a bare git object name is not a base64 blob', () => {
  it('keeps a SHA-1 query value', () => {
    expect(isRedacted(url('commit=' + SHA1))).toBe(false)
  })

  it('still redacts a 64-hex (SHA-256 / app secret shaped) query value', () => {
    expect(isRedacted(url('commit=' + SHA256))).toBe(true)
  })

  it('keeps a SHA after a routed prefix and two SHAs in one query', () => {
    expect(isRedacted(url('changes=Commit:pkg/mainline:' + SHA1))).toBe(false)
    expect(isRedacted(url('from=' + SHA1 + '&to=' + SHA1.split('').reverse().join('')))).toBe(false)
  })

  it('keeps the link when it is a markdown link target', () => {
    const u = url('commit=' + SHA1)
    expect(sanitizeExfiltrationUrls(`[history](${u})`)).toBe(`[history](${u})`)
  })

  it.each([
    ['41 hex', SHA1 + 'a'],
    ['65 hex', SHA256 + '0'],
    ['mixed case', SHA1.slice(0, 20) + SHA1.slice(0, 20).toUpperCase()],
    ['uppercase', SHA1.toUpperCase()],
    ['padding', SHA1 + '='],
    ['glued to a letter', 'x' + SHA1],
    ['glued to a slash run', SHA1 + '/' + SHA1.slice(0, 3)],
    ['percent-encoded separator glued (its hex octet joins the run)', 'pkg%3A' + SHA1],
    ['48 hex, no git length', '0'.repeat(48)],
    ['40 lowercase non-hex', 'abcdefghij'.repeat(4)],
  ])('still redacts a neighbouring shape: %s', (_name, value) => {
    expect(isRedacted(url('v=' + value))).toBe(true)
  })

  it('redacts when a real payload or a fixed credential sits beside the SHA', () => {
    expect(isRedacted(url('commit=' + SHA1 + '&data=' + 'Ab3x'.repeat(10)))).toBe(true)
    expect(isRedacted(url('commit=' + SHA1 + '&k=AKIAIOSFODNN7EXAMPLE'))).toBe(true)
  })

  it('leaves the aggregate length gate untouched', () => {
    const many = Array.from({ length: 5 }, (_, i) => `c${i}=` + SHA1).join('&')
    expect(many.length).toBeGreaterThanOrEqual(200)
    expect(isRedacted(url(many))).toBe(true)
  })
})
