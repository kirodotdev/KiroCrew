import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
import { copyToClipboard } from '../utils/clipboard'
import { buildShareableUrl, copySessionLink, resolveMsgIndex } from '../utils/shareUrl'

describe('buildShareableUrl', () => {
  it('builds URL with sid param', () => {
    const url = buildShareableUrl('chat-1-abc')
    expect(url).toBe(`${window.location.origin}/chat?sid=chat-1-abc`)
  })

  it('includes slug from title', () => {
    const url = buildShareableUrl('chat-1-abc', 'Debug video playback')
    expect(url).toContain('/chat/debug-video-playback?sid=chat-1-abc')
  })

  it('omits slug when title equals key', () => {
    const url = buildShareableUrl('chat-1-abc', 'chat-1-abc')
    expect(url).toBe(`${window.location.origin}/chat?sid=chat-1-abc`)
  })

  it('generates kebab-case slug from title', () => {
    const url = buildShareableUrl('k', 'Fix: Login & Auth (v2)!')
    expect(url).toContain('/chat/fix-login-auth-v2')
  })

  it('truncates slug to 80 chars', () => {
    const longTitle = 'a'.repeat(100)
    const url = buildShareableUrl('k', longTitle)
    const path = new URL(url).pathname
    const slug = path.replace('/chat/', '')
    expect(slug.length).toBeLessThanOrEqual(80)
  })

  it('strips leading and trailing hyphens from slug', () => {
    const url = buildShareableUrl('k', '---hello---')
    expect(url).toContain('/chat/hello?')
  })

  it('includes msg param when messageTs provided', () => {
    const url = buildShareableUrl('chat-1-abc', 'Title', '2025-05-13T14:00:00.000Z')
    expect(url).toContain('sid=chat-1-abc')
    expect(url).toContain('msg=2025-05-13T14')
  })

  it('omits msg param when messageTs not provided', () => {
    const url = buildShareableUrl('chat-1-abc', 'Title')
    expect(url).not.toContain('msg=')
  })

  it('uses /chat base path for orchestrator mode (unified view)', () => {
    const url = buildShareableUrl('orch-1', 'Plan migration', undefined, 'orchestrator')
    expect(url).toContain('/chat/plan-migration?sid=orch-1')
  })

  it('uses /chat base path for default mode', () => {
    const url = buildShareableUrl('chat-1', 'Title', undefined, undefined)
    expect(url).toContain('/chat/title?sid=chat-1')
  })

  it('includes mid param when mid provided', () => {
    const url = buildShareableUrl('chat-1', 'Title', '2025-05-13T14:00:00.000Z', undefined, 'msg-abc-123')
    expect(url).toContain('mid=msg-abc-123')
    expect(url).toContain('msg=2025-05-13T14')
    expect(url).toContain('sid=chat-1')
  })

  it('omits mid param when mid not provided', () => {
    const url = buildShareableUrl('chat-1', 'Title', '2025-05-13T14:00:00.000Z')
    expect(url).not.toContain('mid=')
  })
})

describe('copySessionLink', () => {
  beforeEach(() => vi.clearAllMocks())

  it('calls copyToClipboard with the built URL', async () => {
    await copySessionLink('chat-1-abc', 'My Session')
    expect(copyToClipboard).toHaveBeenCalledWith(
      expect.stringContaining('/chat/my-session?sid=chat-1-abc')
    )
  })

  it('includes message timestamp when provided', async () => {
    await copySessionLink('chat-1-abc', 'Title', '2025-05-13T14:00:00.000Z')
    expect(copyToClipboard).toHaveBeenCalledWith(
      expect.stringContaining('msg=2025-05-13T14')
    )
  })

  it('includes mid in URL when provided', async () => {
    await copySessionLink('chat-1-abc', 'Title', '2025-05-13T14:00:00.000Z', undefined, 'mid-xyz')
    expect(copyToClipboard).toHaveBeenCalledWith(
      expect.stringContaining('mid=mid-xyz')
    )
  })

  it('omits mid from URL when not provided', async () => {
    await copySessionLink('chat-1-abc', 'Title', '2025-05-13T14:00:00.000Z')
    expect(copyToClipboard).toHaveBeenCalledWith(
      expect.not.stringContaining('mid=')
    )
  })
})

describe('resolveMsgIndex', () => {
  const ts = '2025-05-13T14:00:00.000Z'
  const msgs = [
    { ts, meta: { mid: 'mid-A' } },
    { ts, meta: { mid: 'mid-B' } },        // same ts, different mid
    { ts: '2025-05-14T00:00:00.000Z', meta: { mid: 'mid-C' } },
  ]

  it('returns index by mid when mid matches', () => {
    expect(resolveMsgIndex(msgs, ts, 'mid-B')).toBe(1)
  })

  it('resolves to first mid match even when ts also matches an earlier entry', () => {
    expect(resolveMsgIndex(msgs, ts, 'mid-B')).toBe(1)
  })

  it('falls back to ts when mid not provided', () => {
    // Without mid, returns first ts match (index 0)
    expect(resolveMsgIndex(msgs, ts)).toBe(0)
  })

  it('falls back to ts when mid has no match', () => {
    expect(resolveMsgIndex(msgs, ts, 'mid-unknown')).toBe(0)
  })

  it('returns -1 when neither mid nor ts matches', () => {
    expect(resolveMsgIndex(msgs, 'nope', 'nope-mid')).toBe(-1)
  })

  it('returns -1 for empty message list', () => {
    expect(resolveMsgIndex([], ts, 'mid-A')).toBe(-1)
  })

  it('handles messages without meta gracefully', () => {
    const noMeta = [{ ts }, { ts, meta: { mid: 'mid-A' } }]
    expect(resolveMsgIndex(noMeta, ts, 'mid-A')).toBe(1)
  })
})
