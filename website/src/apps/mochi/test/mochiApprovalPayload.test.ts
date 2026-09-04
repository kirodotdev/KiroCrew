/**
 * A user can type the internal approval marker.
 *
 * `__approval__` is written by the approval route, but the composer echoes the
 * user's own message locally — so `__approval__whatever` reached a bare
 * `JSON.parse` and the uncaught SyntaxError took the entire chat panel down to
 * its error boundary. A payload that is not really ours must fall through and
 * render as the text the user actually wrote.
 */
import { describe, expect, it } from 'vitest'

import { externalApprovalApproved, parseApproval } from '../src/renderer/ChatPanel'

describe('parseApproval', () => {
  it('parses a real approval payload', () => {
    const req = parseApproval(JSON.stringify({ id: 'a1', tool: 'fs_read', toolInput: 'x' }))
    expect(req).toEqual({ id: 'a1', tool: 'fs_read', toolInput: 'x' })
  })

  it('returns null for text that is not JSON at all', () => {
    expect(parseApproval('whatever the user typed')).toBeNull()
  })

  it('returns null for JSON that is not an object', () => {
    // `__approval__"hi"` parses fine; reading `.tool` off it would render
    // `undefined` into the bubble instead of the user's message.
    expect(parseApproval('"hi"')).toBeNull()
    expect(parseApproval('42')).toBeNull()
    expect(parseApproval('null')).toBeNull()
  })

  it('returns null when the required fields are missing or wrong-typed', () => {
    expect(parseApproval(JSON.stringify({ tool: 'fs_read' }))).toBeNull()
    expect(parseApproval(JSON.stringify({ id: 'a1' }))).toBeNull()
    expect(parseApproval(JSON.stringify({ id: 1, tool: 'fs_read' }))).toBeNull()
  })
})

describe('externalApprovalApproved', () => {
  it('treats an explicit approved:true as approved', () => {
    expect(externalApprovalApproved({ approved: true })).toBe(true)
  })

  it('never reads a reject as approved (the fabricated-decision bug)', () => {
    expect(externalApprovalApproved({ approved: false })).toBe(false)
    expect(externalApprovalApproved({ id: 'a1', approved: false })).toBe(false)
  })

  it('treats a missing/malformed frame as NOT approved (fail closed)', () => {
    expect(externalApprovalApproved(undefined)).toBe(false)
    expect(externalApprovalApproved({})).toBe(false)
    expect(externalApprovalApproved({ approved: 'true' })).toBe(false)
  })
})
