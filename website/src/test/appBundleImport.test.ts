import { describe, expect, it, vi } from 'vitest'

// An app bundle reads window.__kirocrew_modules while it imports, and the
// dashboard no longer registers it at startup (it holds every lucide icon), so
// the loader every app bundle goes through must load it first.
const order: string[] = []
vi.mock('../app-sdk/shared-modules', () => {
  order.push('registry')
  return {}
})

import { importAppBundle } from '../components/appBundleImport'

describe('importAppBundle', () => {
  it('loads the shared-modules registry before the app bundle', async () => {
    await expect(importAppBundle('/apps/missing-app/ui/index.mjs')).rejects.toBeTruthy()
    expect(order).toEqual(['registry'])
  })
})
