import { apiTransport } from './apiTransport'

export type OrganizationRole = 'conductor' | 'manager' | 'engineer' | 'researcher'
export interface OrganizationMember {
  id: string
  name: string
  role: OrganizationRole
  manager_id: string | null
  state: string
  permissions: string[]
}
export interface OrganizationTask {
  id: string
  parent_id: string | null
  sender: string
  recipient: string
  title: string
  acceptance: string
  state: string
  report: string
  decision: string
}
export interface OrganizationSnapshot {
  settings: {
    revision: number
    concurrency: number
    enabled: boolean
    staffing: Record<OrganizationRole, Partial<Record<OrganizationRole, number>>>
  }
  members: OrganizationMember[]
  tasks: OrganizationTask[]
  messages: { id: string; sender: string; recipient: string; text: string }[]
  runs: { id: string; member_id: string; state: string; error: string }[]
  runtime: { ready: boolean; backend: string; reason: string }
}

export const organizationQuery = {
  queryKey: ['organization'] as const,
  queryFn: async (): Promise<OrganizationSnapshot> =>
    apiTransport.j(await apiTransport.get('/api/organization')) as Promise<OrganizationSnapshot>,
  staleTime: 1000,
  refetchInterval: 2000,
}

export async function organizationAction(
  action: string, values: Record<string, unknown> = {},
): Promise<{ ok: boolean; member_id?: string; task_id?: string }> {
  return apiTransport.j(
    await apiTransport.post('/api/organization', { action, ...values }),
  ) as Promise<{ ok: boolean; member_id?: string; task_id?: string }>
}
