// NewSpecView — full-page conversational spec creator. The name is auto-derived
// from the description (no name field); the project folder is chosen with the
// system-picker-style ProjectPicker (no raw typing by default); the fresh-worktree
// option is an OPT-IN checkbox (unchecked, no "recommended" label) shown only
// for git repos.
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Sparkles, Wrench, Zap, GitBranch, Check } from 'lucide-react'
import { specApi, slugify } from '../api'
import { ACCENT, SEL_BG, inputStyle, Btn } from './shared'
import Clickable from '../../../components/Clickable'
import ProjectPicker from './ProjectPicker'

import { i18nT } from '../../../i18n/t'
const SPEC_TYPES = [
  {
    id: 'feature', Icon: Sparkles,
    titleKey: 'apps.specBuilder.components.newSpecView.type_feature_title',
    blurbKey: 'apps.specBuilder.components.newSpecView.type_feature_blurb',
  },
  {
    id: 'bug', Icon: Wrench,
    titleKey: 'apps.specBuilder.components.newSpecView.type_bug_title',
    blurbKey: 'apps.specBuilder.components.newSpecView.type_bug_blurb',
  },
  {
    id: 'quick', Icon: Zap,
    titleKey: 'apps.specBuilder.components.newSpecView.type_quick_title',
    blurbKey: 'apps.specBuilder.components.newSpecView.type_quick_blurb',
  },
] as const

export interface NewSpecViewProps {
  onCancel: () => void
  onCreated: (name: string) => void
  setErr: (msg: string) => void
  onSettings: () => void
}

export default function NewSpecView({ onCancel, onCreated, setErr, onSettings }: NewSpecViewProps) {
  const [desc, setDesc] = useState('')
  const [wd, setWd] = useState('')
  const [type, setType] = useState<string>('feature')
  const [busy, setBusy] = useState(false)
  const [useWorktree, setUseWorktree] = useState(false)
  const autoName = slugify(desc)

  // Detect whether the chosen folder is a git repo (enables the worktree opt-in).
  // React Query rather than a manual fetch + stale flag: it keys the result by
  // folder, so switching folders quickly cannot let an earlier response land
  // last and mislabel the current one (repo `use-react-query` rule).
  const gitQuery = useQuery({
    queryKey: ['spec-builder', 'browse', wd],
    queryFn: ({ signal }) => specApi.browse(wd, signal),
    enabled: !!wd,
  })
  const isGit: boolean | null = !wd || gitQuery.isPending ? null : !!gitQuery.data?.is_git

  const create = async () => {
    setBusy(true)
    const payload = (n: string) => ({
      name: n, working_dir: wd.trim(), spec_type: type, description: desc, use_worktree: !!(isGit && useWorktree),
    })
    try {
      try {
        await specApi.create(payload(autoName))
        onCreated(autoName)
      } catch (e) {
        // Auto-retry once with a numeric suffix on a name collision. Trim the
        // base first so the suffix survives the 48-char cap — appending to an
        // already-capped slug and slicing would send the same name twice.
        if (/already exists/i.test((e as Error).message)) {
          const alt = autoName.slice(0, 44) + '-' + (Date.now() % 1000)
          await specApi.create(payload(alt))
          onCreated(alt)
        } else throw e
      }
    } catch (e) { setErr((e as Error).message); setBusy(false) }
  }

  // Readiness reflects the fields the USER fills in. The derived name must
  // not veto submission: slugify now always yields a non-empty, backend-valid
  // name (issue #3002 — a Korean-only description used to filter to an empty
  // slug and keep the button permanently disabled).
  const ready = desc.trim().length > 0 && wd.trim().length > 0

  return (
    <div className="flex-1 min-h-0 overflow-y-auto">
      <div className="mx-auto" style={{ maxWidth: '680px', padding: '28px 10px 60px' }}>
        <div className="text-[20px] font-semibold mb-1.5">{i18nT('apps.specBuilder.components.newSpecView.let_s_plan_something')}</div>
        <div className="text-[13px] text-muted mb-7 leading-relaxed">
          {i18nT('apps.specBuilder.components.newSpecView.tell_me_what_you_have_in_mind_i_ll_ask_a_few_cla')}
        </div>

        <label htmlFor="sb-desc" className="block text-[14px] font-semibold mb-2">
          {i18nT('apps.specBuilder.components.newSpecView.what_do_you_want_to_do')}
          <textarea
            id="sb-desc"
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            autoFocus
            rows={3}
            aria-label={i18nT('apps.specBuilder.components.newSpecView.describe_what_you_want_to_do')}
            placeholder={i18nT('apps.specBuilder.components.newSpecView.e_g_add_login_with_google_so_users_don_t_need_pa')}
            style={{ ...inputStyle, resize: 'vertical', marginTop: '8px', marginBottom: '24px', lineHeight: 1.5, fontWeight: 400 }}
          />
        </label>

        <div className="text-[14px] font-semibold mb-2">{i18nT('apps.specBuilder.components.newSpecView.which_project_is_this_for')}</div>
        <ProjectPicker value={wd} onChange={setWd} />

        {!wd && (
          <div className="flex gap-2.5 items-center px-3.5 py-2.5 rounded-lg mb-6 -mt-2.5 text-[12px] text-muted leading-relaxed opacity-80" style={{ border: '1px dashed var(--border)' }}>
            <GitBranch className="lucide-inline text-accent" />
            <span>{i18nT('apps.specBuilder.components.newSpecView.fresh_worktree_option_pick_a_project_folder_abov')}</span>
          </div>
        )}

        {wd && isGit === false && (
          <div className="flex gap-2.5 items-center px-3.5 py-2.5 rounded-lg mb-6 -mt-2.5 text-[12px] text-muted leading-relaxed" style={{ border: '1px dashed var(--border)' }}>
            <GitBranch className="lucide-inline text-accent" />
            <span>{i18nT('apps.specBuilder.components.newSpecView.this_folder_isn_t_a_git_repository_so_i_ll_work')}</span>
          </div>
        )}

        {isGit && (
          <Clickable
            onClick={() => setUseWorktree(!useWorktree)}
            aria-pressed={useWorktree}
            aria-label={i18nT('apps.specBuilder.components.newSpecView.work_in_a_fresh_worktree')}
            className="flex items-start gap-3 px-4 py-3.5 rounded-lg cursor-pointer mb-6 -mt-2.5 focus-ring"
            style={{ border: '1px solid ' + (useWorktree ? ACCENT : 'var(--border)'), background: useWorktree ? SEL_BG : 'var(--card)' }}
          >
            <span
              aria-hidden="true"
              className="w-4 h-4 rounded inline-flex items-center justify-center shrink-0"
              style={{ border: '1px solid ' + (useWorktree ? ACCENT : 'var(--border)'), background: useWorktree ? ACCENT : 'transparent', marginTop: '2px' }}
            >
              {useWorktree && <Check className="lucide-inline text-accent-fg" />}
            </span>
            <div>
              <div className="text-[13px] font-semibold text-text">{i18nT('apps.specBuilder.components.newSpecView.work_in_a_fresh_worktree')}</div>
              <div className="text-[12px] leading-relaxed mt-1 text-muted">
                {i18nT('apps.specBuilder.components.newSpecView.keeps_your_main_checkout_untouched_branch', { branch: 'spec/' + (desc.trim() ? autoName : '…') })}
              </div>
            </div>
          </Clickable>
        )}

        <div className="text-[14px] font-semibold mb-2.5" id="sb-kind-label">{i18nT('apps.specBuilder.components.newSpecView.what_kind_of_work_is_it')}</div>
        <div className="flex gap-3 mb-6 flex-wrap" role="group" aria-labelledby="sb-kind-label">
          {SPEC_TYPES.map(({ id, Icon, titleKey, blurbKey }) => (
            <Clickable
              key={id}
              onClick={() => setType(id)}
              aria-pressed={type === id}
              aria-label={i18nT(titleKey) + ' — ' + i18nT(blurbKey)}
              className="px-4 py-3.5 rounded-lg cursor-pointer focus-ring"
              style={{ flex: '1 1 180px', border: '1px solid ' + (type === id ? ACCENT : 'var(--border)'), background: type === id ? SEL_BG : 'var(--card)' }}
            >
              <div className="text-[14px] font-semibold mb-1 text-text flex items-center gap-[7px]">
                <Icon className="lucide-inline text-accent" />
                {i18nT(titleKey)}
              </div>
              <div className="text-[12px] leading-relaxed text-muted">{i18nT(blurbKey)}</div>
            </Clickable>
          ))}
        </div>

        <div className="flex gap-3 items-center">
          <Btn label={busy ? i18nT('apps.specBuilder.components.newSpecView.starting') : i18nT('apps.specBuilder.components.newSpecView.start_the_conversation')} primary big disabled={busy || !ready} onClick={create} />
          <Btn label={i18nT('apps.specBuilder.components.newSpecView.never_mind')} onClick={onCancel} />
        </div>
        <div className="text-[12px] text-muted mt-5 leading-relaxed">
          {i18nT('apps.specBuilder.components.newSpecView.your_plan_is_saved_as_plain_markdown_inside_your')}{' '}
          <Clickable onClick={onSettings} className="inline underline cursor-pointer focus-ring" style={{ color: ACCENT }}>{i18nT('apps.specBuilder.components.newSpecView.settings')}</Clickable>.
        </div>
      </div>
    </div>
  )
}
