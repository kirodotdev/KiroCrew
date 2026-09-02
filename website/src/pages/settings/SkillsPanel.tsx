import { useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { api } from '../../api/client'
import { useOptimisticConfigPaths, setConfigPathValue } from './useOptimisticConfigPaths'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
type SkillsCfg = { auto_create_from_sessions?: boolean; approval_required?: boolean }

/**
 * Settings → Skills: opt in to automatic skill generation from sessions.
 *
 * Auto-generation is OFF by default. When enabled, completed sessions are
 * analyzed and candidate skills are staged to the pending queue (reviewable on
 * the Skills tab) — they never go live without approval unless "Require
 * approval" is turned off.
 */
export function SkillsPanel() {
  const qc = useQueryClient()
  const [saveError, setSaveError] = useState('')
  // Which path produced the current banner: a retry on the SAME path clears
  // its stale failure, but a save on the other toggle says nothing about
  // whether this one persisted, so its failure stays up.
  const saveErrorPathRef = useRef<string | null>(null)

  const cfgQ = useQuery<{ skills?: SkillsCfg }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const skills = cfgQ.data?.skills
  // Both toggles PATCH the shared ['kirocrewConfig'] object. A whole-object
  // onMutate snapshot here would capture ANOTHER PANEL's in-flight optimistic
  // value on the same query key and restore it on failure (the panel's own
  // two toggles are mutually disabled while a save is pending, so the
  // within-panel interleave is unreachable). Each toggle instead renders
  // `shown(path, server)`; lifecycle in useOptimisticConfigPaths.ts.
  const overlay = useOptimisticConfigPaths(qc)
  const autoCreate = overlay.shown('skills.auto_create_from_sessions', skills?.auto_create_from_sessions ?? false)
  // approval_required defaults ON — generated skills stay gated behind review.
  const approvalRequired = overlay.shown('skills.approval_required', skills?.approval_required ?? true)

  const patchMut = useMutation(overlay.mutationOpts<{ path: string; value: boolean }>({
    queryKey: ['kirocrewConfig'],
    mutationFn: ({ path, value }) => api.patchConfig(path, value),
    path: v => v.path,
    displayValue: v => v.value,
    applyToCache: (cached, { path, value }) => setConfigPathValue(cached as { skills?: SkillsCfg }, path, value),
    onFailure: (_err, { path }) => {
      saveErrorPathRef.current = path
      setSaveError(i18nT('pages.settings.skillsPanel.failed_to_save_skills_setting'))
    },
    onSupersede: path => {
      if (saveErrorPathRef.current === path) {
        saveErrorPathRef.current = null
        setSaveError('')
      }
    },
  }))

  const disabled = cfgQ.isLoading || patchMut.isPending

  return (
    <SettingsSection title={i18nT('pages.settings.skillsPanel.skills')}>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.skillsPanel.auto_generate_skills_from_sessions')}
          description={i18nT('pages.settings.skillsPanel.analyze_each_completed_session_and_draft_a_reusa')}
          checked={autoCreate}
          onChange={(v) => patchMut.mutate({ path: 'skills.auto_create_from_sessions', value: v })}
          disabled={disabled}
          configKey="skills.auto_create_from_sessions"
        />
        <SettingsToggle
          label={i18nT('pages.settings.skillsPanel.require_approval_before_generated_skills_go_live')}
          description={i18nT('pages.settings.skillsPanel.keep_every_auto_generated_candidate_in_the_pendi')}
          checked={approvalRequired}
          onChange={(v) => patchMut.mutate({ path: 'skills.approval_required', value: v })}
          disabled={disabled || !autoCreate}
          configKey="skills.approval_required"
        />
      </SettingsCard>
      <ErrorNotice message={saveError} className="mt-2" askAgent />
    </SettingsSection>
  )
}
