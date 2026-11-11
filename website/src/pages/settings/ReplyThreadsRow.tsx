/**
 * Settings > Developer > Feature Previews > Crew and bots: the **Reply threads on
 * bot chat messages** row -- `dashboard.crewmate_threads`, reply threads on Bot
 * chat messages (RFC crewmates launch, screen 07), off by default.
 *
 * One row of that group's card (`FeaturePreviewsSection.tsx`), so it renders a
 * single root element: the switch with its own load and save notices under it.
 *
 * Server-side, like the Power toggle on the Chat tab: the value is PATCHed to
 * `dashboard.crewmate_threads` and read back through the shared
 * `['kirocrewConfig']` query the Members page reads the flag from
 * (`useCrewmateThreadsFlag`), so the Reply in thread control appears or goes
 * on the next render without a reload.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { SettingsToggle } from '../../components/settings'
import { CREWMATE_THREADS_CONFIG_KEY } from '../../hooks/useCrewmateThreadsFlag'
import { i18nT } from '../../i18n/t'

type KirocrewConfigShape = { dashboard?: { crewmate_threads?: boolean } }

export function ReplyThreadsRow() {
  const qc = useQueryClient()
  const cfgQ = useQuery<KirocrewConfigShape>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const [saveError, setSaveError] = useState('')
  const threads = cfgQ.data?.dashboard?.crewmate_threads === true
  const threadsMut = useMutation({
    mutationFn: (v: boolean) => api.patchConfig(CREWMATE_THREADS_CONFIG_KEY, v),
    onSuccess: () => {
      setSaveError('')
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
    onError: () => setSaveError(i18nT('pages.settings.crewmatesSection.failed_to_save')),
  })

  return (
    <div>
      <SettingsToggle
        label={i18nT('pages.settings.crewmatesSection.reply_threads')}
        hint={i18nT('pages.settings.crewmatesSection.reply_threads_desc')}
        checked={threads}
        onChange={(v) => threadsMut.mutate(v)}
        disabled={!cfgQ.isSuccess || threadsMut.isPending}
        configKey={CREWMATE_THREADS_CONFIG_KEY}
      />
      {/* A config read that failed leaves the switch disabled; say so, with the
          query's own retry, rather than showing a control that cannot move.
          askAgent: a toggle holds no draft, so the hand-off loses nothing. */}
      {cfgQ.isError && (
        <ErrorNotice
          message={i18nT('pages.settings.crewmatesSection.failed_to_load')}
          askAgent
          className="mt-2"
          testId="crewmates-config-error"
          footer={
            <Btn onClick={() => { void cfgQ.refetch() }} disabled={cfgQ.isFetching} data-testid="crewmates-config-retry">
              {i18nT('pages.settings.crewmatesSection.retry')}
            </Btn>
          }
        />
      )}
      {saveError && (
        <ErrorNotice
          message={saveError}
          onDismiss={() => setSaveError('')}
          askAgent
          className="mt-2"
          testId="crewmates-save-error"
        />
      )}
    </div>
  )
}
