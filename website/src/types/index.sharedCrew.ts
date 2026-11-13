// Shared types for Shared Crew attribution: the browser-facing principal and
// what a session row says about who (or what) started it. Mirrors
// `kiro_crew.shared_crew` (contracts.py, attribution.py, trigger.py); the owning
// spec is docs/system-specs/modules/shared-crew.md. Consumers import them from
// `types/index.ts`; `api/sharedCrew.ts` re-exports them beside the rest of the
// Shared Crew wire contracts.

export type PrincipalKind = 'human' | 'machine' | 'system' | 'unknown'

export interface DisplaySnapshot {
  name: string
  login: string
  captured_at: number | null
}

/** Browser-facing principal (`Principal.to_public_dict`). */
export interface PublicPrincipal {
  id: string
  kind: PrincipalKind
  display: DisplaySnapshot
}

export type AttributionAvailability = 'recorded' | 'legacy_missing' | 'unattributed'

export type SessionTriggerKind = 'scheduled' | 'webhook' | 'system'

/** What started a session no person started (`shared_crew.trigger`). A label,
 *  never a person: `configured_by` is a verified principal only when recorded. */
export interface SessionTrigger {
  kind: SessionTriggerKind
  ref: string
  name: string
  configured_by: PublicPrincipal | null
}

export interface SessionAttribution {
  creator: PublicPrincipal
  availability: AttributionAvailability
  created_at: number | null
  trigger?: SessionTrigger
}
