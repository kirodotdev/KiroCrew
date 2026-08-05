import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
import { copyToClipboard } from '../utils/clipboard'
import { buildShareableUrl, copySessionLink } from '../utils/shareUrl'

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
})
