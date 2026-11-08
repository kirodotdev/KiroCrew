/**
 * The Meetings settings view opts back into focus-refetch with a finite staleTime.
 *
 * The dashboard's query defaults set `staleTime: Infinity`, so focus-refetch is a
 * no-op unless a surface sets its own (`src/test/bootRequests.test.ts` pins the
 * defaults and the core opt-ins). The settings view builds a full-replace PUT of
 * the meetings config from its query cache, so a stale cache there is data loss:
 * it must keep a finite staleTime.
 */
import { describe, it, expect } from 'vitest'

import settingsSource from './SettingsView.tsx?raw'

describe('Meetings settings focus-refetch opt-in', () => {
  it('sets a finite staleTime (full-replace PUT of meetings config)', () => {
    expect(settingsSource).toMatch(/staleTime:\s*\d+/)
  })
})
