import { createContext, useContext, type ReactNode } from 'react'
import { getAdapter } from './registry'
import type { ProviderAdapter } from './types'

// KiroACP-only: kiro-cli over ACP is the sole provider, so there is exactly one
// adapter and no provider selection. The context is retained (rather than
// inlining the adapter at each call site) so the many useProvider() consumers
// stay unchanged.
const acpAdapter = getAdapter()

const ProviderContext = createContext<ProviderAdapter>(acpAdapter)

export function ProviderProvider({ children }: { children: ReactNode }) {
  return <ProviderContext.Provider value={acpAdapter}>{children}</ProviderContext.Provider>
}

export function useProvider(): ProviderAdapter {
  return useContext(ProviderContext)
}
