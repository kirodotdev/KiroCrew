import { describe, it, expect, beforeEach } from 'vitest'

import {
  PREVIEW_ACP_BACKENDS,
  PREVIEW_FLAG_PREFIX,
  readPreviewFlag,
  setPreviewFlag,
} from '../utils/previewFlags'

describe('PREVIEW_ACP_BACKENDS', () => {
  beforeEach(() => localStorage.clear())

  it('is OFF when nothing was ever set', () => {
    // The whole point of the gate: an operator who has not opted in must not be
    // shown a selector whose happy path has never been observed working.
    expect(readPreviewFlag(PREVIEW_ACP_BACKENDS)).toBe(false)
  })

  it('carries the shared prefix so cross-tab listeners see it', () => {
    // `usePreviewFlagRevision` matches on the prefix rather than a flag list, so
    // a flag missing the prefix silently stops updating other tabs.
    expect(PREVIEW_ACP_BACKENDS.startsWith(PREVIEW_FLAG_PREFIX)).toBe(true)
  })

  it('is distinct from the webhooks flag', () => {
    setPreviewFlag(PREVIEW_ACP_BACKENDS, true)
    expect(localStorage.getItem(`${PREVIEW_FLAG_PREFIX}webhooks`)).toBeNull()
  })

  it('reads OFF for any value that is not exactly "1"', () => {
    // Fails closed on a hand-edited or partially-written key.
    for (const raw of ['0', 'true', 'yes', '', 'null']) {
      localStorage.setItem(PREVIEW_ACP_BACKENDS, raw)
      expect(readPreviewFlag(PREVIEW_ACP_BACKENDS)).toBe(false)
    }
  })

  it('round-trips on and back off', () => {
    expect(setPreviewFlag(PREVIEW_ACP_BACKENDS, true)).toBe(true)
    expect(readPreviewFlag(PREVIEW_ACP_BACKENDS)).toBe(true)
    expect(setPreviewFlag(PREVIEW_ACP_BACKENDS, false)).toBe(true)
    expect(readPreviewFlag(PREVIEW_ACP_BACKENDS)).toBe(false)
  })
})
