import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { PackageX } from 'lucide-react'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'

// — Restore surface for built-in skills the user deleted.
//
// Deleting a skill that ships with Kiro Crew is recorded so the gateway's
// startup sync stops reinstalling it. Without this list that record would be
// invisible and the only way back would be hand-editing a dotfile. It renders
// NOTHING when no built-in is uninstalled, so a default install shows no extra
// chrome.

interface Props {
  // False while the host tab latches an unsaved skill edit: the agent hand-off
  // navigates to the chat, unmounting the tab and discarding that draft.
  askAgent?: boolean
}

export default function UninstalledBuiltinSkillsList({ askAgent = true }: Props = {}) {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const { data, isError } = useQuery<{ uninstalled?: string[] }>({
    queryKey: ['skills-uninstalled'],
    // Defensive call: many suites partial-mock the api client, leaving a newly
    // added method undefined on a mount-time fetch.
    queryFn: () => Promise.resolve(api.uninstalledBuiltinSkills?.()).then(r => r ?? {}),
    staleTime: 0,
    refetchOnMount: 'always',
  })

  // A failed read must not look like "nothing to restore": that would hide the
  // restore controls with no sign anything went wrong. No hand-off while the
  // host tab latches an unsaved skill edit (see Props.askAgent).
  if (isError) {
    return (
      <div className="mb-3" data-testid="uninstalled-builtin-skills-error">
        <ErrorNotice
          message={i18nT('components.uninstalledBuiltinSkillsList.load_failed')}
          askAgent={askAgent}
        />
      </div>
    )
  }

  const names = Array.isArray(data?.uninstalled) ? data.uninstalled : []
  if (names.length === 0) return null

  const restore = async (name: string) => {
    setBusy(name)
    setError(null)
    try {
      await api.restoreBuiltinSkill(name)
      await queryClient.invalidateQueries({ queryKey: ['skills-uninstalled'] })
      await queryClient.invalidateQueries({ queryKey: ['skills'] })
    } catch {
      setError(name)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="mb-3 rounded-lg border border-border p-3" data-testid="uninstalled-builtin-skills">
      <h4 className="text-sm font-semibold text-text-strong mb-1 flex items-center gap-2">
        <PackageX size={14} className="lucide-inline" />
        {i18nT('components.uninstalledBuiltinSkillsList.title')}
      </h4>
      <p className="text-[11px] text-muted mb-2.5">
        {i18nT('components.uninstalledBuiltinSkillsList.hint')}
      </p>
      <ul className="flex flex-col gap-1.5">
        {names.map(name => (
          <li key={name} className="flex items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="text-[12px] font-mono truncate text-text" title={name}>{name}</div>
              {error === name && (
                <ErrorNotice
                  variant="inline"
                  message={i18nT('components.uninstalledBuiltinSkillsList.restore_failed')}
                  askAgent={askAgent}
                />
              )}
            </div>
            <Btn onClick={() => restore(name)} disabled={busy === name}>
              {busy === name
                ? i18nT('components.uninstalledBuiltinSkillsList.restoring')
                : i18nT('components.uninstalledBuiltinSkillsList.restore')}
            </Btn>
          </li>
        ))}
      </ul>
    </div>
  )
}
