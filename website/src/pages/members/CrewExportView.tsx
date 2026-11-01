import { useEffect, useState, type ReactNode } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Download } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api, type CrewmateExportChoices, type CrewmateExportPreview } from '../../api/client'
import { apiErrorCode } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Toggle } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { downloadBlob } from '../../utils/download'

/** Why the server left a part out, worded for the owner. Each code is one literal key. */
function reasonLabel(reason: string): string {
  switch (reason) {
    case 'script_missing': return i18nT('pages.membersPage.export_reason_script_missing')
    case 'script_refused': return i18nT('pages.membersPage.export_reason_script_refused')
    case 'script_shared': return i18nT('pages.membersPage.export_reason_script_shared')
    case 'shell_command': return i18nT('pages.membersPage.export_reason_shell_command')
    case 'credential': return i18nT('pages.membersPage.export_reason_credential')
    case 'control_chars': return i18nT('pages.membersPage.export_reason_control_chars')
    case 'too_large': return i18nT('pages.membersPage.export_reason_too_large')
    case 'bundle_full': return i18nT('pages.membersPage.export_reason_bundle_full')
    case 'link': return i18nT('pages.membersPage.export_reason_link')
    case 'subfolder': return i18nT('pages.membersPage.export_reason_subfolder')
    case 'excluded_file': return i18nT('pages.membersPage.export_reason_excluded_file')
    case 'no_skill_md': return i18nT('pages.membersPage.export_reason_no_skill_md')
    case 'name_taken': return i18nT('pages.membersPage.export_reason_name_taken')
    case 'custom_template': return i18nT('pages.membersPage.export_reason_custom_template')
    case 'first_goal_only': return i18nT('pages.membersPage.export_reason_first_goal_only')
    default: return i18nT('pages.membersPage.export_reason_other')
  }
}

function kindLabel(kind: string): string {
  switch (kind) {
    case 'cron': return i18nT('pages.membersPage.export_kind_cron')
    case 'skill': return i18nT('pages.membersPage.export_kind_skill')
    case 'rules': return i18nT('pages.membersPage.export_rules')
    case 'goals': return i18nT('pages.membersPage.export_goals')
    case 'description': return i18nT('pages.membersPage.export_kind_description')
    case 'avatar': return i18nT('pages.membersPage.export_kind_avatar')
    case 'dashboard': return i18nT('pages.membersPage.export_kind_dashboard')
    default: return kind
  }
}

/** Everything a plan shows the owner and puts in the file, contents included, so a
 *  skill file, prompt, script or rule edited after the view loaded is a change too.
 *  The lessons skill counts as one flag: its text is the owner's own editor. */
function planSignature(plan: CrewmateExportPreview): string {
  return JSON.stringify({
    name: plan.name,
    description: plan.description,
    model: plan.model,
    dashboard: plan.dashboard,
    avatar: plan.avatar,
    avatar_digest: plan.avatar_digest,
    rules: plan.rules,
    goals: plan.goals,
    crons: plan.crons,
    // Code-point order: a stable key for the comparison, not a display order.
    skills: plan.skills.filter(s => !s.lessons).sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0)),
    lessons: plan.skills.some(s => s.lessons),
  })
}

function Code({ text, testId }: { text: string; testId?: string }) {
  return (
    <pre className="mt-1.5 max-h-48 overflow-auto rounded-lg bg-bg-elevated border border-border px-2.5 py-2 text-[11.5px] leading-snug whitespace-pre-wrap break-words font-mono" data-testid={testId}>{text}</pre>
  )
}

function Line({ label, value, toggle, children, testId }: {
  label: string
  value?: string
  toggle?: { checked: boolean; onChange: (v: boolean) => void }
  children?: ReactNode
  testId: string
}) {
  return (
    <div className="border-t border-border first:border-t-0 px-3.5 py-2.5" data-testid={testId}>
      <div className="flex items-center gap-3">
        <span className="flex-1 min-w-0 text-[13px] font-semibold">{label}</span>
        {value !== undefined && <span className="text-[12px] text-muted truncate">{value}</span>}
        {toggle && <Toggle checked={toggle.checked} onChange={toggle.onChange} label={label} />}
      </div>
      {children}
    </div>
  )
}

/**
 * The Export confirm view, pushed over the profile card (rfc-crewmate-share.md §5.3).
 * It lists exactly what the file holds, read from the server's own plan, with the
 * §5.3 defaults: rules, goals and crons on, lessons off. The press downloads the file;
 * nothing is saved on the server and the lessons text is never stored there.
 */
export default function CrewExportView({ slug, member, onDirtyChange }: {
  slug: string
  member: string
  /** Whether the lessons editor holds text, so the card can ask before a pop drops it. */
  onDirtyChange?: (dirty: boolean) => void
}) {
  const { t } = useTranslation()
  const [rules, setRules] = useState(true)
  const [goals, setGoals] = useState(true)
  const [crons, setCrons] = useState(true)
  const [skippedSkills, setSkippedSkills] = useState<Set<string>>(new Set())
  const [lessonsOn, setLessonsOn] = useState(false)
  const [lessons, setLessons] = useState('')
  // The crewmate drafts once, when the toggle first turns on; the owner edits.
  const draft = useMutation({
    mutationFn: () => api.memberExportLessonsDraft(slug, member),
    onSuccess: r => setLessons(prev => (prev.trim() ? prev : r.text)),
  })
  // Typed text counts while the toggle is off too: the view keeps it, and an exit drops it.
  const dirty = lessons.trim() !== ''
  useEffect(() => { onDirtyChange?.(dirty) }, [dirty, onDirtyChange])
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange])
  const turnLessons = (on: boolean) => {
    setLessonsOn(on)
    if (on && !lessons.trim() && !draft.isPending) draft.mutate()
  }
  // 409 is the one refusal with its own words: the crewmate has no memory of its own.
  const draftError = (draft.error as { status?: number } | null)?.status === 409
    ? t('pages.membersPage.export_lessons_no_memory')
    : draft.isError ? t('pages.membersPage.export_lessons_draft_failed') : ''
  const preview = useQuery<CrewmateExportPreview>({
    queryKey: ['crewmate-export', slug, member],
    queryFn: () => api.memberExportPreview(slug, member),
    staleTime: 0,
    gcTime: 0,
  })
  // The plan for the CURRENT choices, read again on the press. When it no longer
  // matches what the view listed (a file changed, or the lessons skill took a name or
  // the last room in the file), the press stops and the view lists that plan instead,
  // so the next press ships only what is on screen.
  const [checked, setChecked] = useState<{ plan: CrewmateExportPreview } | null>(null)
  // A changed choice makes the last re-read stale; the next press reads again.
  useEffect(() => { setChecked(null) }, [rules, goals, crons, skippedSkills, lessonsOn, lessons])
  const choicesFor = (view: CrewmateExportPreview): CrewmateExportChoices => ({
    rules,
    goals,
    crons,
    skills: view.skills.filter(s => !s.lessons && !skippedSkills.has(s.name)).map(s => s.name),
    lessons: lessonsOn && lessons.trim() ? lessons : null,
  })
  const save = useMutation({
    mutationFn: async (view: CrewmateExportPreview) => {
      const choices = choicesFor(view)
      const plan = await api.memberExportPreview(slug, member, choices)
      const sig = planSignature(plan)
      const expected = planSignature({
        ...view,
        rules: choices.rules ? view.rules : null,
        goals: choices.goals ? view.goals : null,
        crons: choices.crons ? view.crons : [],
        skills: [
          ...(choices.lessons ? [{ name: '', lessons: true, files: {} }] : []),
          ...view.skills.filter(s => choices.skills?.includes(s.name)),
        ],
      })
      if (sig !== expected) {
        setChecked({ plan })
        return
      }
      let blob: Blob
      try {
        // Bound to the plan just checked: the server refuses (409) a file whose
        // content moved since, so nothing unseen ships.
        blob = await api.memberExportFile(slug, member, { ...choices, confirm: plan.fingerprint })
      } catch (err) {
        if ((err as { status?: number } | null)?.status !== 409) throw err
        setChecked({ plan: await api.memberExportPreview(slug, member, choices) })
        return
      }
      downloadBlob(blob, `${view.name}.crewjam`)
    },
  })

  if (preview.isPending) {
    return <div className="px-4 py-4 text-[13px] text-muted" data-testid="crew-export-loading">{t('pages.membersPage.export_loading')}</div>
  }
  if (preview.isError || !preview.data) {
    return (
      <div className="px-4 py-4">
        <ErrorNotice message={t('pages.membersPage.export_load_failed')} askAgent testId="crew-export-load-error" />
      </div>
    )
  }
  const v = checked?.plan ?? preview.data
  const leftOut = v.left_out
  // Only a `lessons_` code is the lessons text failing a check; any other refusal is
  // about the crewmate, so it gets the general message.
  const saveCode = apiErrorCode(save.error)
  const saveError = saveCode === 'lessons_exfil_url'
    ? t('pages.membersPage.export_lessons_exfil_url')
    : saveCode === 'lessons_too_large'
      ? t('pages.membersPage.export_lessons_too_large')
      : saveCode.startsWith('lessons_')
        ? t('pages.membersPage.export_lessons_refused')
        : t('pages.membersPage.export_failed')
  const ownSkills = v.skills.filter(s => !s.lessons)
  const toggleSkill = (name: string, on: boolean) => setSkippedSkills(prev => {
    const next = new Set(prev)
    if (on) next.delete(name)
    else next.add(name)
    return next
  })

  return (
    <div className="px-4 py-4 flex flex-col gap-3" data-testid="crew-export-view">
      <p className="text-[12.5px] text-muted">{t('pages.membersPage.export_intro')}</p>
      <div className="rounded-2xl border border-border bg-bg overflow-hidden">
        <Line label={t('pages.membersPage.export_identity')} value={t('pages.membersPage.export_always')} testId="crew-export-identity">
          <p className="mt-1 text-[12.5px] text-text">{v.name}</p>
          {v.description && <p className="text-[12px] text-muted whitespace-pre-wrap">{v.description}</p>}
        </Line>
        <Line label={t('pages.membersPage.export_rules')} toggle={{ checked: rules, onChange: setRules }} testId="crew-export-rules">
          {rules && (v.rules ? <Code text={v.rules} /> : <p className="mt-1 text-[12px] text-muted">{t('pages.membersPage.export_none')}</p>)}
        </Line>
        <Line label={t('pages.membersPage.export_goals')} toggle={{ checked: goals, onChange: setGoals }} testId="crew-export-goals">
          {goals && (v.goals ? (
            <div className="mt-1 text-[12.5px]">
              <p className="whitespace-pre-wrap">{v.goals.longTerm}</p>
              {v.goals.keyResults.length > 0 && (
                <ul className="mt-1 list-disc pl-4 text-muted">
                  {v.goals.keyResults.map((kr, i) => <li key={i}>{kr.text}{kr.target ? ` · ${kr.target}` : ''}</li>)}
                </ul>
              )}
            </div>
          ) : <p className="mt-1 text-[12px] text-muted">{t('pages.membersPage.export_none')}</p>)}
        </Line>
        <Line label={t('pages.membersPage.export_crons')} toggle={{ checked: crons, onChange: setCrons }} testId="crew-export-crons">
          {crons && (v.crons.length ? v.crons.map(c => (
            <div key={c.name} className="mt-1.5" data-testid="crew-export-cron">
              <p className="text-[12.5px]">{c.name}{c.timezone ? <span className="text-muted"> · {c.timezone}</span> : null}</p>
              {c.message && <Code text={c.message} testId="crew-export-cron-message" />}
              {c.script && <Code text={c.source ?? ''} testId="crew-export-cron-script" />}
            </div>
          )) : <p className="mt-1 text-[12px] text-muted">{t('pages.membersPage.export_none')}</p>)}
        </Line>
        <Line label={t('pages.membersPage.export_model')} value={v.model || t('pages.membersPage.export_model_default')} testId="crew-export-model" />
        {ownSkills.map(s => (
          <Line
            key={s.name}
            label={t('pages.membersPage.export_skill', { name: s.name })}
            toggle={{ checked: !skippedSkills.has(s.name), onChange: on => toggleSkill(s.name, on) }}
            testId="crew-export-skill"
          >
            {!skippedSkills.has(s.name) && Object.entries(s.files).map(([file, text]) => (
              <div key={file} className="mt-1.5">
                <p className="text-[12px] text-muted font-mono">{file}</p>
                <Code text={text} />
              </div>
            ))}
          </Line>
        ))}
        <Line label={t('pages.membersPage.export_lessons')} toggle={{ checked: lessonsOn, onChange: turnLessons }} testId="crew-export-lessons">
          {lessonsOn && (
            <>
              <p className="mt-1 text-[12px] text-muted">{t('pages.membersPage.export_lessons_hint')}</p>
              {draft.isPending && <p className="mt-1 text-[12px] text-muted" data-testid="crew-export-lessons-drafting">{t('pages.membersPage.export_lessons_drafting', { name: v.name })}</p>}
              {/* No hand-off: the editor below holds the owner's lessons text, which a
                  hand-off to chat would unmount and lose. */}
              {draftError && <ErrorNotice message={draftError} testId="crew-export-lessons-draft-error" />}
              <textarea
                value={lessons}
                onChange={e => setLessons(e.target.value)}
                aria-label={t('pages.membersPage.export_lessons')}
                placeholder={t('pages.membersPage.export_lessons_placeholder')}
                rows={8}
                disabled={draft.isPending}
                className="mt-1.5 w-full rounded-lg border border-border bg-bg-elevated px-2.5 py-2 text-[12.5px] font-mono focus-ring"
                data-testid="crew-export-lessons-editor"
              />
            </>
          )}
        </Line>
        <Line
          label={t('pages.membersPage.export_dashboard')}
          value={v.dashboard
            ? v.dashboard
            : leftOut.some(i => i.kind === 'dashboard')
              ? t('pages.membersPage.export_dashboard_custom')
              : t('pages.membersPage.export_dashboard_default')}
          testId="crew-export-dashboard"
        />
        <Line label={t('pages.membersPage.export_mcp')} testId="crew-export-mcp">
          {v.mcp_servers.length === 0 ? (
            <p className="mt-1 text-[12px] text-muted">{t('pages.membersPage.export_none')}</p>
          ) : (
            <>
              {!v.mcp_in_file && <p className="mt-1 text-[12px] text-warn-text">{t('pages.membersPage.export_mcp_not_yet')}</p>}
              <ul className="mt-1 text-[12.5px]">
                {v.mcp_servers.map(m => (
                  <li key={m.server}>
                    {m.server}
                    {m.placeholders.length > 0 && <span className="text-muted"> · {t('pages.membersPage.export_mcp_placeholders', { keys: m.placeholders.join(', ') })}</span>}
                  </li>
                ))}
              </ul>
            </>
          )}
        </Line>
      </div>
      {leftOut.length > 0 && (
        <section className="rounded-2xl border border-border bg-bg px-3.5 py-3" data-testid="crew-export-left-out">
          <div className="text-[11px] font-semibold tracking-wide uppercase text-muted mb-1">{t('pages.membersPage.export_left_out')}</div>
          <ul className="text-[12.5px] flex flex-col gap-0.5">
            {leftOut.map((i, n) => (
              <li key={n}>{kindLabel(i.kind)}{i.name ? ` “${i.name}”` : ''}: <span className="text-muted">{reasonLabel(i.reason)}</span></li>
            ))}
          </ul>
        </section>
      )}
      <p className="text-[12.5px] text-muted" data-testid="crew-export-not-included">{t('pages.membersPage.export_not_included')}</p>
      {checked && !save.isPending && (
        <p className="text-[12.5px] text-warn-text" role="status" data-testid="crew-export-plan-changed">{t('pages.membersPage.export_plan_changed')}</p>
      )}
      {/* No hand-off: the lessons editor above may hold unsaved text, which a hand-off
          to chat would unmount and lose. */}
      {save.isError && <ErrorNotice message={saveError} testId="crew-export-error" />}
      <Btn primary type="button" onClick={() => save.mutate(v)} disabled={save.isPending} className="self-start h-9 px-4 rounded-full" data-testid="crew-export-download">
        <Download size={14} aria-hidden="true" />
        {t('pages.membersPage.export_download')}
      </Btn>
    </div>
  )
}
