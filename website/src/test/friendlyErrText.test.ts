import { describe, it, expect } from 'vitest'
import { friendlyErrText } from '../api/client'

describe('friendlyErrText', () => {
  it('unwraps {"error": …} to the human message with real newlines', () => {
    const body = JSON.stringify({ error: 'line one\n  sudo do-thing\nline two' })
    const out = friendlyErrText(500, body)
    expect(out).toBe('line one\n  sudo do-thing\nline two')
    // No raw JSON envelope / escaped sequences leak through.
    expect(out).not.toContain('{"error"')
    expect(out).not.toContain('\\n')
  })

  it('falls back to detail/message fields', () => {
    expect(friendlyErrText(500, JSON.stringify({ detail: 'boom' }))).toBe('boom')
    expect(friendlyErrText(500, JSON.stringify({ message: 'kaboom' }))).toBe('kaboom')
  })

  it('returns the raw body when it is not JSON', () => {
    expect(friendlyErrText(500, 'plain text error')).toBe('plain text error')
  })

  it('returns the raw body when JSON has no known message field', () => {
    const body = JSON.stringify({ code: 42 })
    expect(friendlyErrText(500, body)).toBe(body)
  })

  it('keeps the 429 friendly message', () => {
    expect(friendlyErrText(429, '{"error":"x"}')).toContain('Rate limited')
  })

  it('reports no message for an HTML error page, whichever doctype it carries', () => {
    // The whole document used to become the message, so a tunnel 502 rendered as
    // `<!DOCTYPE html><html><head><meta charset="utf…` inside the error banner.
    expect(friendlyErrText(502, '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>502</body></html>')).toBe('')
    expect(friendlyErrText(503, '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN">\n<html><body>oops</body></html>')).toBe('')
    expect(friendlyErrText(502, '<html>\n<head><title>502 Bad Gateway</title></head>\n</html>')).toBe('')
    expect(friendlyErrText(502, '\n  <!doctype html><html></html>')).toBe('')
  })

  it('leaves a non-HTML markup body alone, so a widened guard cannot swallow a reason', () => {
    const xml = '<?xml version="1.0"?><Error><Message>Access Denied</Message></Error>'
    expect(friendlyErrText(403, xml)).toBe(xml)
    expect(friendlyErrText(400, 'expected <html> but got json')).toBe('expected <html> but got json')
  })
})
