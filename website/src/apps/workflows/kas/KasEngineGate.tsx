/**
 * The one gate every KAS-native workflow surface goes through: `GET
 * /api/workflows/engine` must answer `kas`. Anything else renders `fallback`
 * (the Crew surface, or nothing).
 *
 * One gateway-wide opt-in now (`agent.kas_workflows`), folded into the engine
 * answer server-side, so there is no per-device preview flag to read: the gate
 * is the single engine probe. While it is in flight the fallback shows rather
 * than a blank — on a Crew backend that is the final answer, so it must not
 * flash away.
 */
import type { ReactNode } from 'react'

import { useKasWorkflowsEnabled } from './useKasWorkflows'

export function KasEngineGate({ children, fallback = null }: { children: ReactNode; fallback?: ReactNode }) {
  const { enabled, loading } = useKasWorkflowsEnabled()
  if (loading || !enabled) return <>{fallback}</>
  return <>{children}</>
}
