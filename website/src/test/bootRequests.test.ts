import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { queryClient } from '../api/queryClient'

/**
 * Guards for the round-trip reductions in the dashboard's boot path. Each of
 * these costs a full round-trip per occurrence when the dashboard is reached
 * over a tunnel, so a regression here is a user-visible slowdown, not a style
 * nit.
 */

describe('query defaults', () => {
  it('uses Infinity staleTime so focus-refetch is effectively disabled globally', () => {
    // Live data arrives by WebSocket push (invalidateQueries on server events).
    // With staleTime: Infinity, queries never go stale on their own, so
    // refetchOnWindowFocus (which only fires on stale queries) is a no-op for
    // any query that doesn't explicitly override staleTime.
    expect(queryClient.getDefaultOptions().queries?.staleTime).toBe(Infinity)
  })

  it('keeps refetchOnWindowFocus at the safe default (true/undefined)', () => {
    // We do NOT set refetchOnWindowFocus: false. The safe default stays.
    // Focus-refetch is neutralized via staleTime, not by disabling the option.
    const val = queryClient.getDefaultOptions().queries?.refetchOnWindowFocus
    expect(val === true || val === undefined).toBe(true)
  })

  it('keeps the retry policy', () => {
    const q = queryClient.getDefaultOptions().queries
    expect(q?.retry).toBeTypeOf('function')
  })
})

describe('focus-refetch opt-ins via finite staleTime', () => {
  // Surfaces where focus-refetch is load-bearing set a finite staleTime so the
  // global Infinity doesn't prevent their focus-triggered refresh. Each either
  // builds a REPLACE-ALL write from its query cache (stale = data loss) or
  // documents behavior that depends on focus-triggered refetch.
  const read = (rel: string) =>
    readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

  const optIns: Array<[string, string]> = [
    ['../components/RegistryManager.tsx', 'replace-all PUT of registries'],
    ['../apps/meetings/SettingsView.tsx', 'full-replace PUT of meetings config'],
    ['../apps/personal-shopper/SitesTab.tsx', 'replace-all PUT of shopper sites'],
    ['../apps/papyrus/PapyrusPage.tsx', 'project deleted-in-another-tab detection'],
    ['../apps/code-review-sage/components/LearningRail.tsx', 'replace-all PUT of active namespaces'],
    ['../apps/ops-mission-control/SettingsPanel.tsx', 'replace-all PUT of rotation settings'],
  ]

  it.each(optIns)('%s sets a finite staleTime (%s)', (rel) => {
    const src = read(rel)
    // Must have staleTime with a numeric value (not Infinity)
    expect(src).toMatch(/staleTime:\s*\d+/)
  })
})
