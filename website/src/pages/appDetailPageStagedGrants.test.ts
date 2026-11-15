// `memoryAdmin` opens one API prefix, and only while the memory provider preview runs:
// with it off the gateway never grants the prefix, so it must not wait for approval.
import { describe, expect, it } from 'vitest'

import type { AppInfo } from './appDetailPageTypes'
import { stagedGrantsOf } from './appDetailPageStagedGrants'

const app = {
  manifest: { permissions: { api: ['/api/stream'], memoryAdmin: true } },
  approvedGrants: { api: ['/api/stream'], events: [] },
} as unknown as AppInfo

describe('stagedGrantsOf memoryAdmin', () => {
  it('holds the memory prefix for approval only while memory providers run', () => {
    expect(stagedGrantsOf(app, true).stagedApi).toEqual(['/api/memory/provider'])
    expect(stagedGrantsOf(app, false).stagedApi).toEqual([])
    expect(stagedGrantsOf(app, false).hasStagedGrants).toBe(false)
  })
})
