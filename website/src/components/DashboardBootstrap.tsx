import type { ReactNode } from 'react'
import { useRefreshScheduler } from '../hooks/useRefreshScheduler'
import KiroPrerequisiteGate from './KiroPrerequisiteGate'

export default function DashboardBootstrap({ children }: { children: ReactNode }) {
  // This must mount outside the prerequisite gate: a stale access cookie may
  // otherwise prevent App from mounting the scheduler that repairs that cookie.
  useRefreshScheduler()
  return <KiroPrerequisiteGate>{children}</KiroPrerequisiteGate>
}
