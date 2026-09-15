import { describe, it, expect } from 'vitest'
import { safeHttpUrl } from './safeUrl'

describe('safeHttpUrl', () => {
  it('accepts http URLs', () => {
    expect(safeHttpUrl('http://example.com')).toBe('http://example.com')
  })
  it('accepts https URLs', () => {
    expect(safeHttpUrl('https://d123.cloudfront.net/my-app/')).toBe('https://d123.cloudfront.net/my-app/')
  })
  it('rejects javascript: scheme', () => {
    expect(safeHttpUrl('javascript:alert(1)')).toBeNull()
  })
  it('rejects data: scheme', () => {
    expect(safeHttpUrl('data:text/html,<script>alert(1)</script>')).toBeNull()
  })
  it('rejects invalid URLs', () => {
    expect(safeHttpUrl('not a url')).toBeNull()
  })
  it('rejects empty string', () => {
    expect(safeHttpUrl('')).toBeNull()
  })

  it('rejects URLs with Basic-auth userinfo (R21 F2)', () => {
    expect(safeHttpUrl('https://user:pass@attacker.example/')).toBeNull()
    expect(safeHttpUrl('https://user@attacker.example/')).toBeNull()
  })
})
