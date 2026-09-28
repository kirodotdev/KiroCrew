import { apiTransport } from './apiTransport'

export type CapabilitySection = 'mcpServers' | 'tools' | 'allowedTools' | 'autoApprove' | 'skills' | 'prompt' | 'model' | 'resources'
export interface CapabilityRef { section: CapabilitySection; id: string }
export interface CapabilityRow extends CapabilityRef {
  label: string
  state: 'inherited' | 'local' | 'removed'
  present: boolean
  value: unknown
  managed?: boolean
  shared_reference?: boolean
}
export interface CapabilityView {
  schema_version: 1
  member: string
  mode: 'shared' | 'legacy_snapshot' | 'inherited'
  revision: string
  template: { name: string; source: 'builtin' | 'package' | 'custom' | 'project' | 'unknown'; scope: 'global' | 'project'; available: boolean; error_code?: string }
  rows: CapabilityRow[]
  connections: { id: string; label: string; managed: boolean }[]
  skills: { id: string; label: string; shared_reference: boolean }[]
  parent_changes: (CapabilityRef & { kind: 'added' | 'changed' | 'removed'; conflict: boolean; requires_approval: boolean; before: unknown; after: unknown })[]
  runtime: { status: 'pending' | 'applied' | 'failed' | 'unverified'; saved_revision: string; sessions: unknown[]; error_code?: string }
}
export type CapabilityOperation = CapabilityRef & (
  | { action: 'inherit' | 'remove' }
  | { action: 'set'; value: unknown; connection_id?: never; retain_paths?: string[] }
  | { action: 'set'; connection_id: string; value?: never; retain_paths?: never }
)
export interface CapabilityDraft {
  revision: string
  enroll: boolean
  operations: CapabilityOperation[]
  accept_parent: CapabilityRef[]
  accept_members: string[]
}
export interface CapabilityPreview extends CapabilityView {
  preview_token: string
  impact: (CapabilityRef & { member: string; change: 'added' | 'removed' | 'changed'; approval_expanded: boolean })[]
}
const endpoint = (member: string) => `/api/agents/${encodeURIComponent(member)}/capabilities`
const { get, post, put, j } = apiTransport
export const crewCapabilitiesApi = {
  get: (member: string): Promise<CapabilityView> => get(endpoint(member)).then(j) as Promise<CapabilityView>,
  preview: (member: string, draft: CapabilityDraft): Promise<CapabilityPreview> =>
    post(`${endpoint(member)}/preview`, draft).then(j) as Promise<CapabilityPreview>,
  save: (member: string, draft: CapabilityDraft & { preview_token: string }): Promise<CapabilityView> =>
    put(endpoint(member), draft).then(j) as Promise<CapabilityView>,
}
export const crewCapabilitiesKey = (member: string) => ['crew-capabilities', member] as const
export const capabilityKey = (row: CapabilityRef) => JSON.stringify([row.section, row.id])
