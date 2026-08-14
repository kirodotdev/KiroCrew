// Settings: what model reviews, how hard it thinks, how many run at once.
//
// Replaces the old <details> disclosure with a proper view. Every control writes
// through immediately (there is no Save button to forget), and each one says what
// it actually affects — "Concurrency: 5" means nothing without the sentence under
// it.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Settings as SettingsIcon } from 'lucide-react'

import { sageApi } from '../api'
import type { Settings } from '../lib/types'

import SimpleSelect from '../../../components/SimpleSelect'
import { i18nT } from '../../../i18n/t'
function Field({ label, hint, children }: {
  label: string
  hint: string
  children: React.ReactNode
}) {
  return (
    <div className="pb-4 mb-4 border-b border-border last:border-b-0 last:mb-0 last:pb-0">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        {/* Not a <label>: the control lives in `children` and already carries its
            own aria-label, so a label element here would have nothing to bind to. */}
        <span className="text-[13px] font-semibold text-text">{label}</span>
        {children}
      </div>
      <div className="text-[12px] text-muted mt-1.5 leading-[1.5] max-w-[560px]">{hint}</div>
    </div>
  )
}

const SELECT_CLASS =
  'text-[12.5px] px-2 py-1 rounded-md bg-bg-elevated text-text border border-border '
  + 'outline-none focus:border-accent cursor-pointer'

export default function SettingsView() {
  const qc = useQueryClient()
  const settingsQuery = useQuery({
    queryKey: ['code-review-sage', 'settings'],
    queryFn: () => sageApi.settings(),
  })
  const saveMut = useMutation({
    mutationFn: (patch: Partial<Settings>) => sageApi.putSettings(patch),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['code-review-sage', 'settings'] })
      // The reviewer identity shown in the rail comes from /runs, so it has to be
      // refreshed too or the rail keeps showing the previous model.
      void qc.invalidateQueries({ queryKey: ['code-review-sage', 'runs'] })
    },
  })

  const data = settingsQuery.data
  const s = data?.settings

  return (
    <div className="h-full overflow-y-auto scrollbar-none px-6 py-6">
      <div className="max-w-[720px]">
        <h1 className="text-[22px] font-bold leading-tight text-text-strong flex items-center gap-2">
          <SettingsIcon size={18} className="text-accent" aria-hidden="true" /> {i18nT('apps.codeReviewSage.views.settingsView.settings')}
        </h1>
        <p className="text-[13px] text-muted mt-1.5 leading-[1.5]">
          {i18nT('apps.codeReviewSage.views.settingsView.these_apply_to_every_review_started_from_here')}
        </p>

        {settingsQuery.isLoading && (
          <div className="mt-6 inline-flex items-center gap-2 text-[13px] text-muted">
            <Loader2 size={14} className="animate-spin motion-reduce:animate-none" />
            {i18nT('apps.codeReviewSage.views.settingsView.loading_settings')}
          </div>
        )}
        {settingsQuery.error && (
          <div className="mt-6 text-[13px] text-danger">
            {(settingsQuery.error as Error).message}
          </div>
        )}

        {data && s && (
          <div className="mt-6 rounded-xl border border-border bg-card px-4 py-4">
            <Field
              label={i18nT('apps.codeReviewSage.views.settingsView.model')}
              hint={i18nT('apps.codeReviewSage.views.settingsView.which_model_performs_the_review_default_inherits')}
            >
              <SimpleSelect
                aria-label={i18nT('apps.codeReviewSage.views.settingsView.review_model')}
                options={data.models ?? []}
                value={s.model ?? ''}
                onChange={(v) => saveMut.mutate({ model: v || null })}
                clearLabel={i18nT('apps.codeReviewSage.views.settingsView.default_agent_config')}
                className={SELECT_CLASS}
              />
            </Field>

            <Field
              label={i18nT('apps.codeReviewSage.views.settingsView.reasoning_effort')}
              hint={i18nT('apps.codeReviewSage.views.settingsView.higher_effort_finds_more_costs_more_and_takes_lo')}
            >
              <SimpleSelect
                aria-label={i18nT('apps.codeReviewSage.views.settingsView.reasoning_effort')}
                options={data.efforts ?? []}
                value={s.effort ?? ''}
                onChange={(v) => saveMut.mutate({ effort: v })}
                clearLabel={i18nT('apps.codeReviewSage.views.settingsView.default_model_provider')}
                className={SELECT_CLASS}
              />
            </Field>

            <Field
              label={i18nT('apps.codeReviewSage.views.settingsView.reviews_at_once')}
              hint={i18nT('apps.codeReviewSage.views.settingsView.how_many_pull_requests_are_reviewed_in_parallel')}
            >
              <SimpleSelect
                aria-label={i18nT('apps.codeReviewSage.views.settingsView.reviews_at_once')}
                options={Array.from(
                  { length: data.max_concurrent_max ?? 30 },
                  (_, i) => String(i + 1),
                )}
                value={String(s.max_concurrent ?? 5)}
                onChange={(v) => saveMut.mutate({ max_concurrent: Number(v) })}
                className={SELECT_CLASS}
              />
            </Field>
          </div>
        )}

        {saveMut.error && (
          <div className="mt-3 text-[12.5px] text-danger">
            {(saveMut.error as Error).message}
          </div>
        )}
      </div>
    </div>
  )
}
