import { useRef, useState } from 'react'
import { Link, Navigate, useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsInput } from '../../components/settings'
import { api } from '../../api/client'
import { useOptimisticConfigPaths, setConfigPathValue } from './useOptimisticConfigPaths'
import { stringListLines } from '../../components/settingRef/envShellCommands'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
type SkillsCfg = { auto_create_from_sessions?: boolean; approval_required?: boolean; extra_paths?: string[] }

// The field below spells this key as a literal: the settings registry generator reads
// `configKey` only from a string literal, not from a constant.
const EXTRA_PATHS_KEY = 'skills.extra_paths'

const EMPTY_PATHS: string[] = []

const sameList = (a: readonly string[], b: readonly string[]) =>
  a.length === b.length && a.every((item, i) => item === b[i])

/** Where the pending queue lives — Approve, Dismiss, Dismiss-all and `?review=`. */
const PENDING_QUEUE_ROUTE = '/capabilities?tab=skills'

/**
 * Settings → Skills: opt in to automatic skill generation from sessions.
 *
 * Auto-generation is OFF by default. When enabled, completed sessions are
 * analyzed and candidate skills are staged to the pending queue — they never go
 * live without approval unless "Require approval" is turned off.
 *
 * That queue is rendered by `PendingSkillsPanel` on Agent Capabilities →
 * Skills, which owns Approve, Dismiss and Dismiss-all. This panel is the place
 * the toggles above put candidates INTO it, so it links there rather than
 * mounting a second copy of the list: one queue with one home cannot disagree
 * with itself.
 *
 * `?review=<slug>` belongs to that queue, and the staged-candidate notification
 * addresses it there (`dashboard/server.py`). A `?review=` that arrives HERE is
 * hand-written or bookmarked — #12543 reports doing exactly that, because this
 * page's own copy pointed at "the Skills tab" — so it is handed on rather than
 * silently ignored.
 */
export function SkillsPanel() {
  const qc = useQueryClient()
  const [saveError, setSaveError] = useState('')
  // Which path produced the current banner: a retry on the SAME path clears
  // its stale failure, but a save on the other toggle says nothing about
  // whether this one persisted, so its failure stays up.
  const saveErrorPathRef = useRef<string | null>(null)
  // The extra-paths field keeps its own failure: it sits beside an unsaved draft the
  // generic banner below must not be mistaken for, and a toggle save says nothing
  // about whether this list persisted.
  const [extraPathsError, setExtraPathsError] = useState('')
  // What the user has typed and not yet saved, or null while the field simply shows
  // the stored list. Null is what lets a refetch refresh the field without ever
  // replacing text the user is still editing.
  const [extraPathsDraft, setExtraPathsDraft] = useState<string | null>(null)
  const [params] = useSearchParams()
  const reviewSlug = params.get('review')

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

  const storedExtraPaths = overlay.shown(EXTRA_PATHS_KEY, skills?.extra_paths ?? EMPTY_PATHS)

  const patchMut = useMutation(overlay.mutationOpts<{ path: string; value: boolean | string[] }>({
    queryKey: ['kirocrewConfig'],
    mutationFn: ({ path, value }) => api.patchConfig(path, value),
    path: v => v.path,
    displayValue: v => v.value,
    applyToCache: (cached, { path, value }) => setConfigPathValue(cached as { skills?: SkillsCfg }, path, value),
    onFailure: (_err, { path }) => {
      if (path === EXTRA_PATHS_KEY) {
        setExtraPathsError(i18nT('pages.settings.skillsPanel.failed_to_save_extra_skill_paths'))
        return
      }
      saveErrorPathRef.current = path
      setSaveError(i18nT('pages.settings.skillsPanel.failed_to_save_skills_setting'))
    },
    onSupersede: path => {
      if (path === EXTRA_PATHS_KEY) {
        setExtraPathsError('')
        return
      }
      if (saveErrorPathRef.current === path) {
        saveErrorPathRef.current = null
        setSaveError('')
      }
    },
  }))

  const disabled = cfgQ.isLoading || cfgQ.isError || patchMut.isPending

  // Save on blur: one trim / drop-empty rule (shared with the command a Settings
  // reference shows) turns the text into the list, and an unchanged list is not written.
  // A success clears the draft only if the text still normalises to what was saved, so
  // typing that landed after the request began is kept rather than overwritten.
  const commitExtraPaths = () => {
    if (extraPathsDraft === null) return
    const paths = stringListLines(extraPathsDraft)
    if (sameList(paths, storedExtraPaths)) {
      setExtraPathsDraft(null)
      return
    }
    patchMut.mutate(
      { path: EXTRA_PATHS_KEY, value: paths },
      {
        onSuccess: () =>
          setExtraPathsDraft(current =>
            current !== null && sameList(stringListLines(current), paths) ? null : current,
          ),
      },
    )
  }

  // Hand a hand-written or bookmarked `?review=` to the queue that honours it,
  // rather than rendering a page with no queue on it.
  // `useSearchParams` returns the value DECODED, so re-encoding is what keeps a
  // slug carrying a reserved character from opening a second parameter.
  if (reviewSlug) {
    return <Navigate to={`${PENDING_QUEUE_ROUTE}&review=${encodeURIComponent(reviewSlug)}`} replace />
  }

  return (
    <SettingsSection title={i18nT('pages.settings.skillsPanel.skills')}>
      {/* Load failure: the toggles below would otherwise show defaults as if they
          were the stored values. Toggle-only card, nothing to lose → hand-off on. */}
      {cfgQ.isError && (
        <ErrorNotice message={i18nT('pages.settings.skillsPanel.config_load_failed')} className="mb-2" askAgent />
      )}
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.skillsPanel.auto_generate_skills_from_sessions')}
          hint={i18nT('pages.settings.skillsPanel.analyze_each_completed_session_and_draft_a_reusa')}
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
        {/* Locked while a save is in flight: the field is not offered for typing during
            the round trip, so a slow success cannot land on text it never saw. */}
        <SettingsInput
          label={i18nT('pages.settings.skillsPanel.extra_skill_paths')}
          description={i18nT('pages.settings.skillsPanel.extra_skill_paths_description')}
          placeholder={i18nT('pages.settings.skillsPanel.extra_skill_paths_placeholder')}
          value={extraPathsDraft ?? storedExtraPaths.join('\n')}
          onChange={setExtraPathsDraft}
          onBlur={commitExtraPaths}
          disabled={disabled}
          multiline
          configKey="skills.extra_paths"
        />
        {/* No hand-off: the draft above is unsaved text, and the hand-off's navigation
            would unmount the panel and lose it. A failed save leaves the text in the
            field to retry. */}
        <ErrorNotice message={extraPathsError} variant="inline" />
      </SettingsCard>
      {/* The way out. Naming a destination the reader has to find themselves is
          most of the friction here: the toggles decide what gets staged, and
          nothing on this page can approve what they staged. */}
      <p className="mt-2 text-[12px] text-muted">
        <Link to={PENDING_QUEUE_ROUTE} className="text-accent hover:underline">
          {i18nT('pages.settings.skillsPanel.review_pending_candidates_on_agent_capabilities')}
        </Link>
      </p>
      <ErrorNotice message={saveError} className="mt-2" askAgent />
    </SettingsSection>
  )
}
