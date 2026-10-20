import { useId, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { api, ApiError, friendlyErrText } from '../../api/client'
import type { FollowDefaultCount } from '../../api/client/chatSlotSettings'
import { apiErrorCode } from '../../api/apiError'
import { useConfirm } from '../../components/ConfirmDialog'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsField } from '../../components/settings'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

/** What a failed count or confirm says. A set that moved between the two gets its
 *  own sentence, because pressing again is the remedy and shows the new count. A
 *  confirm that could not save some chats reads as the plain failure: those chats
 *  keep their model, and the next press counts exactly them. */
function followDefaultError(e: unknown): string {
  const failed = i18nT('pages.settings.chatPanel.follow_default_failed')
  if (e instanceof ApiError) {
    const code = apiErrorCode(e)
    if (code === 'chats_changed') return i18nT('pages.settings.chatPanel.follow_default_changed')
    if (code === 'chats_not_saved') return failed
    if (e.authRequired) return e.message || failed
    return friendlyErrText(e.status, e.body) || failed
  }
  return e instanceof Error && e.message ? e.message : failed
}

/** What the confirm says, in reading order: the model the chats land on (the
 *  Default Model setting by name, or what Auto does when it is set to Auto), the
 *  models they leave with how many chats keep each, the first counted chats by
 *  title with how many more, and what cannot be undone. */
function FollowDefaultConfirmBody({ shown }: { shown: FollowDefaultCount }) {
  const modelsId = useId()
  const titlesId = useId()
  const more = shown.count - shown.titles.length
  return (
    <div className="flex flex-col gap-3">
      <p className="m-0" data-testid="follow-default-target">
        {shown.default_model ? (
          <Trans
            i18nKey="pages.settings.chatPanel.follow_default_confirm_target"
            components={{ model: <span className="font-mono">{shown.default_model}</span> }}
          />
        ) : (
          i18nT('pages.settings.chatPanel.follow_default_confirm_target_auto')
        )}
      </p>
      <div className="flex flex-col gap-1">
        <p id={modelsId} className="m-0 font-semibold">
          {i18nT('pages.settings.chatPanel.follow_default_models_heading')}
        </p>
        <ul aria-labelledby={modelsId} className="m-0 flex list-none flex-col gap-0.5 p-0">
          {shown.models.map(row => (
            <li key={row.model} className="flex min-w-0 items-baseline gap-2">
              <span className="min-w-0 truncate font-mono">{row.model}</span>
              <span className="shrink-0 text-muted">
                {i18nT('pages.settings.chatPanel.follow_default_model_chats', { count: row.count })}
              </span>
            </li>
          ))}
        </ul>
      </div>
      {shown.titles.length > 0 && (
        <div className="flex flex-col gap-1">
          <p id={titlesId} className="m-0 font-semibold">
            {i18nT('pages.settings.chatPanel.follow_default_titles_heading')}
          </p>
          <ul aria-labelledby={titlesId} className="m-0 flex list-none flex-col gap-0.5 p-0">
            {shown.titles.map((title, index) => (
              <li key={index} className={title ? 'truncate' : 'truncate text-muted'}>
                {title || i18nT('pages.settings.chatPanel.follow_default_untitled')}
              </li>
            ))}
          </ul>
          {more > 0 && (
            <p className="m-0 text-muted" data-testid="follow-default-more">
              {i18nT('pages.settings.chatPanel.follow_default_more', { count: more })}
            </p>
          )}
        </div>
      )}
      <p className="m-0" data-testid="follow-default-caveat">
        {i18nT('pages.settings.chatPanel.follow_default_confirm_body')}
      </p>
    </div>
  )
}

/**
 * Settings > Chat > Models: make the chats that keep a model of their own follow
 * the default model again.
 *
 * A chat can keep the model it first ran on, and a kept model cannot be told apart
 * from one a person picked, so nothing changes without this press: it counts the
 * chats first, asks with that number, the model they land on, the models they
 * leave and the first few by title, and on confirm the gateway clears exactly the
 * chats it counted. A model picked in a chat afterwards stays.
 */
export function FollowDefaultModelAction() {
  const queryClient = useQueryClient()
  const followDefault = useMutation({
    mutationFn: (digest: string) => api.chatSlotsFollowDefault(digest),
  })
  const { confirm, confirmDialog } = useConfirm()
  // `confirming` keeps the button disabled under its own label while the dialog is open.
  const [phase, setPhase] = useState<'idle' | 'counting' | 'confirming' | 'updating'>('idle')
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')

  const run = async () => {
    setPhase('counting')
    setStatus('')
    setError('')
    try {
      const shown = await queryClient.fetchQuery({
        queryKey: ['chat-slots-follow-default'],
        queryFn: () => api.chatSlotsFollowDefaultCount(),
        staleTime: 0,
      })
      const { count, digest } = shown
      if (count === 0) {
        setStatus(i18nT('pages.settings.chatPanel.follow_default_none'))
        return
      }
      setPhase('confirming')
      const ok = await confirm({
        title: i18nT('pages.settings.chatPanel.follow_default_confirm_title', { count }),
        body: <FollowDefaultConfirmBody shown={shown} />,
        confirmLabel: i18nT('pages.settings.chatPanel.follow_default_confirm'),
        primary: true,
      })
      if (!ok) return
      setPhase('updating')
      const { changed } = await followDefault.mutateAsync(digest)
      setStatus(i18nT('pages.settings.chatPanel.follow_default_done', { count: changed }))
    } catch (e) {
      setError(followDefaultError(e))
    } finally {
      setPhase('idle')
    }
  }

  return (
    <SettingsField
      label={i18nT('pages.settings.chatPanel.follow_default_label')}
      description={i18nT('pages.settings.chatPanel.follow_default_description')}
      hint={i18nT('pages.settings.chatPanel.follow_default_hint')}
    >
      <div className="flex flex-wrap items-center gap-3">
        <Btn type="button" onClick={() => void run()} disabled={phase !== 'idle'} data-testid="follow-default-button">
          {phase === 'counting'
            ? i18nT('pages.settings.chatPanel.follow_default_counting')
            : phase === 'updating'
              ? i18nT('pages.settings.chatPanel.follow_default_updating')
              : i18nT('pages.settings.chatPanel.follow_default_button')}
        </Btn>
        {status && (
          <span role="status" className="text-[12px] text-muted" data-testid="follow-default-status">
            {status}
          </span>
        )}
      </div>
      {/* No hand-off: the remedy is in the message (press again), and the panel
          around this row holds drafts a navigation would discard. */}
      <ErrorNotice variant="inline" message={error} testId="follow-default-error" />
      {confirmDialog}
    </SettingsField>
  )
}
