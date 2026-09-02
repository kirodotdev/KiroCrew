import type { ProviderAdapter, ProviderId } from './types'
import { AcpAdapter } from './adapters/acp'

// KiroCrew is KiroACP-only: kiro-cli over ACP is the sole provider
// (agent.provider enum is ["acp"]). The adapter registry therefore has a
// single entry; it is kept as a thin indirection so the 15+ pages that consume
// useProvider()/the adapter interface for labels/capabilities/model-windows
// stay unchanged.
const ADAPTERS: Record<ProviderId, ProviderAdapter> = {
  acp: new AcpAdapter(),
}

export function getAdapter(_id?: ProviderId): ProviderAdapter {
  return ADAPTERS.acp
}
