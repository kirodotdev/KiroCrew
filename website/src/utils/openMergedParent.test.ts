import { describe, it, expect, vi } from 'vitest'
import { openMergedParent, parentSlotKeyFromForkedFrom } from './openMergedParent'

/**
 * The regression this guards (#3816 UX review, Watch): "Open parent" on a
 * merged fork whose parent is CLOSED (resumed from History, parent never
 * re-opened) must RESUME the parent before switching — a bare switch 404s and
 * unwinds silently. A live parent is switched to directly; a failed resume is
 * surfaced, not swallowed.
 */
describe('openMergedParent', () => {
  const base = () => ({
    parentKey: 'zzq-parent',
    resumeKey: 'dashboard:zzq-parent',
    resume: vi.fn().mockResolvedValue({ ok: true }),
    switchTo: vi.fn(),
    onError: vi.fn(),
  })

  it('switches straight to a LIVE parent without resuming', async () => {
    const d = base()
    await openMergedParent({ ...d, liveSlotKeys: ['zzq-parent', 'other'] })
    expect(d.resume).not.toHaveBeenCalled()
    expect(d.switchTo).toHaveBeenCalledWith('zzq-parent')
    expect(d.onError).not.toHaveBeenCalled()
  })

  it('resumes a CLOSED parent (by the colon resume key) then switches', async () => {
    const d = base()
    await openMergedParent({ ...d, liveSlotKeys: ['other'] })
    expect(d.resume).toHaveBeenCalledWith('dashboard:zzq-parent')
    expect(d.switchTo).toHaveBeenCalledWith('zzq-parent')
    expect(d.onError).not.toHaveBeenCalled()
  })

  it('surfaces the error and does NOT switch when a resume is refused (ok:false)', async () => {
    const d = base()
    d.resume.mockResolvedValue({ ok: false })
    await openMergedParent({ ...d, liveSlotKeys: ['other'] })
    expect(d.onError).toHaveBeenCalledTimes(1)
    expect(d.switchTo).not.toHaveBeenCalled()
  })

  it('surfaces the error and does NOT switch when the resume throws', async () => {
    const d = base()
    const err = new Error('offline')
    d.resume.mockRejectedValue(err)
    await openMergedParent({ ...d, liveSlotKeys: ['other'] })
    expect(d.onError).toHaveBeenCalledWith(err)
    expect(d.switchTo).not.toHaveBeenCalled()
  })
})

describe('parentSlotKeyFromForkedFrom', () => {
  it('strips the dashboard: prefix for a dashboard parent', () => {
    expect(parentSlotKeyFromForkedFrom('dashboard:chat-1-123')).toBe('chat-1-123')
  })
  it('folds a CHANNEL parent key to the live slot spelling (GPT round 8)', () => {
    // The server's _normalize_slot_key folds every char outside [\w.-] to _,
    // so the live slot for a slack:<ts> parent is slack_<ts>-spelled.
    expect(parentSlotKeyFromForkedFrom('slack:C123-456.789')).toBe('slack_C123-456.789')
  })
  it('strips a dashboard_ filename-stem prefix', () => {
    expect(parentSlotKeyFromForkedFrom('dashboard_chat-1-123')).toBe('chat-1-123')
  })
  it('is identity for an already-live folded key', () => {
    expect(parentSlotKeyFromForkedFrom('slack_C123-456.789')).toBe('slack_C123-456.789')
  })
})
