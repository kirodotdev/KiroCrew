import { describe, it, expect } from 'vitest'

import { detectPreviewUrl, previewFeedDecision } from '../utils/detectPreviewUrl'
import type { ChatMessage } from '../types'

const msg = (role: ChatMessage['role'], content: string): ChatMessage =>
  ({ role, content } as ChatMessage)

describe('detectPreviewUrl', () => {
  it('returns null with no preview signal', () => {
    expect(detectPreviewUrl([msg('assistant', 'hello, see https://example.com/docs')])).toBeNull()
  })

  it('picks up an explicit marker as source=marker', () => {
    const out = detectPreviewUrl([
      msg('assistant', 'Server up.\n<!-- kirocrew:preview url="http://127.0.0.1:8080" -->\nDone.'),
    ])
    expect(out).toEqual({ url: 'http://127.0.0.1:8080', source: 'marker' })
  })

  it('falls back to the newest localhost URL as source=heuristic', () => {
    const out = detectPreviewUrl([
      msg('assistant', 'starting on http://localhost:3000'),
      msg('assistant', 'moved it — now serving at http://localhost:5173/'),
    ])
    expect(out).toEqual({ url: 'http://localhost:5173/', source: 'heuristic' })
  })

  it('prefers the marker over an incidental localhost URL WITHIN the same message', () => {
    const out = detectPreviewUrl([
      msg('assistant', 'old dev note http://localhost:9999\n<!-- kirocrew:preview url="http://127.0.0.1:8080" -->'),
    ])
    expect(out).toEqual({ url: 'http://127.0.0.1:8080', source: 'marker' })
  })

  it('lets a NEWER prose URL win over an OLDER marker (chronological, not marker-global)', () => {
    const out = detectPreviewUrl([
      msg('assistant', 'first run\n<!-- kirocrew:preview url="http://127.0.0.1:8080" -->'),
      msg('assistant', 'restarted on a new port — serving at http://localhost:4321/'),
    ])
    expect(out).toEqual({ url: 'http://localhost:4321/', source: 'heuristic' })
  })

  it('ignores URLs the USER pasted (only assistant messages count)', () => {
    expect(detectPreviewUrl([msg('user', 'preview http://localhost:8080')])).toBeNull()
  })

  it('does not match non-loopback hosts in the heuristic', () => {
    expect(detectPreviewUrl([msg('assistant', 'deployed to http://example.com:8080')])).toBeNull()
  })
})

describe('previewFeedDecision (drive-by-request guard)', () => {
  it('returns null when there is no signal', () => {
    expect(previewFeedDecision(null, false)).toBeNull()
  })

  it('opens + loads for an explicit marker, regardless of an existing target', () => {
    expect(previewFeedDecision({ url: 'http://127.0.0.1:8080', source: 'marker' }, false))
      .toEqual({ url: 'http://127.0.0.1:8080', open: true })
    expect(previewFeedDecision({ url: 'http://127.0.0.1:8080', source: 'marker' }, true))
      .toEqual({ url: 'http://127.0.0.1:8080', open: true })
  })

  it('NEVER opens/loads for a heuristic URL — pre-fills only when no target is set', () => {
    // Regression for the drive-by-request vector: a bare localhost URL echoed by
    // the model must not auto-open the panel or fire a GET (open === false).
    expect(previewFeedDecision({ url: 'http://localhost:5173', source: 'heuristic' }, false))
      .toEqual({ url: 'http://localhost:5173', open: false })
  })

  it('ignores a heuristic URL entirely when the session already has a target', () => {
    expect(previewFeedDecision({ url: 'http://localhost:5173', source: 'heuristic' }, true)).toBeNull()
  })
})
