import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { FolderX, ShieldCheck } from 'lucide-react'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { Btn } from './ui'

// — Withdraw surface for project-skills trust grants.
//
// The consent dialog promises the operator can withdraw a grant later, so this
// list is what makes that promise true. It renders NOTHING when no grant
// exists: an empty card would be permanent chrome on a page most operators
// never grant from.
//
// A grant whose directory has since been deleted or moved is still listed
// (`exists: false`). Hiding it would make it invisible AND un-revokable, and a
// directory recreated at the same path would silently inherit the old consent.

interface TrustRow {
  path: string
  granted_at?: number | null
  exists?: boolean
}

interface TrustSnapshot {
  project?: string
  trusted?: boolean
  grants?: TrustRow[]
}

export default function ProjectSkillsTrustList() {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const { data } = useQuery<TrustSnapshot>({
    queryKey: ['skill-trust'],
    // Defensive call: many suites partial-mock the api client, leaving a newly
    // added method undefined on a mount-time fetch.
    queryFn: () => Promise.resolve(api.skillTrust?.()).then(r => (r ?? {}) as TrustSnapshot),
    staleTime: 60 * 1000,
  })

  const grants = Array.isArray(data?.grants) ? data.grants : []
  if (grants.length === 0) return null

  const revoke = async (path: string) => {
    setBusy(path)
    setError(null)
    try {
      await api.revokeSkillTrust(path)
      await queryClient.invalidateQueries({ queryKey: ['skill-trust'] })
      // The catalog's `trusted` flags are now stale — every per-slot ['skills',…]
      // entry must refetch or a revoked folder still reads as usable.
      await queryClient.invalidateQueries({ queryKey: ['skills'] })
    } catch {
      setError(path)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="mb-3 rounded-lg border border-border p-3">
      <h4 className="text-sm font-semibold text-text-strong mb-1 flex items-center gap-2">
        <ShieldCheck size={14} className="lucide-inline" />
        {i18nT('components.projectSkillsTrustList.title')}
      </h4>
      <p className="text-[11px] text-muted mb-2.5">
        {i18nT('components.projectSkillsTrustList.hint')}
      </p>
      <ul className="flex flex-col gap-1.5">
        {grants.map(g => (
          <li key={g.path} className="flex items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="text-[12px] font-mono truncate text-text" title={g.path}>{g.path}</div>
              {g.exists === false && (
                <div className="text-[11px] text-muted flex items-center gap-1">
                  <FolderX size={11} className="lucide-inline" />
                  {i18nT('components.projectSkillsTrustList.folder_missing')}
                </div>
              )}
              {error === g.path && (
                <div role="alert" className="text-[11px] text-warn">
                  {i18nT('components.projectSkillsTrustList.revoke_failed')}
                </div>
              )}
            </div>
            <Btn onClick={() => revoke(g.path)} disabled={busy === g.path}>
              {busy === g.path
                ? i18nT('components.projectSkillsTrustList.revoking')
                : i18nT('components.projectSkillsTrustList.revoke')}
            </Btn>
          </li>
        ))}
      </ul>
    </div>
  )
}
