/**
 * Chat Status Tags — the app homepage.
 *
 * One thing to do here: view and edit the prompt the HOURLY RECONCILER runs. That
 * cron reads the prompt to decide which review systems it inspects and how it
 * promotes a chat (e.g. to `review` when it owns an open pull request, to `done`
 * when they all merge) — so editing this text is how an operator changes what the
 * reconciler checks, such as tweaking its GitHub PR behavior.
 *
 * This is a BUILTIN dashboard page (rendered by BuiltinAppRoute inside the main
 * React tree), so it uses same-origin `fetch` with the dashboard's session cookie
 * via ./api — NOT the app-sdk hooks, which only wrap standalone/installed apps.
 *
 * Backend contract (built in parallel):
 *   GET  /api/apps/chat-status-tags/reconcile-prompt
 *        -> { prompt, isDefault, defaultPrompt }
 *   PUT  same path, body { prompt }  -> same shape
 *        (an empty string resets to the default; 403 when the app is disabled)
 */
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CalendarClock, RotateCcw, Save, ShieldCheck, Wrench } from 'lucide-react'
import { Badge, Btn, Card, ContentSkeleton, PageHeader, Toggle } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import {
  chatStatusTagsApi,
  ChatStatusTagsApiError,
  type AutomationSettings,
  type AutomationSettingsPatch,
  type ReconcileCron,
  type ReconcilePrompt,
} from './api'

const QUERY_KEY = ['chat-status-tags', 'reconcile-prompt'] as const
const SETTINGS_KEY = ['chat-status-tags', 'settings'] as const

/**
 * The two automation switches. Both behaviors cost model credits, so a
 * credit-conscious operator can turn either off; both ship enabled.
 *
 * Each switch owns its own mutation keyed on which field it writes, so the two
 * never share a pending or error state: the switch is disabled only while ITS
 * OWN write is in flight, and a failed write surfaces its own inline message.
 * The PUT body is the partial for that one field; the response is the full
 * fresh state, which we write straight into the settings cache. A 503 on the
 * reconciler write means the scheduler is unavailable — a distinct message from
 * a generic failure.
 */
function AutomationSection() {
  const queryClient = useQueryClient()

  const query = useQuery<AutomationSettings, ChatStatusTagsApiError>({
    queryKey: SETTINGS_KEY,
    queryFn: () => chatStatusTagsApi.fetchSettings(),
  })

  const useToggleMutation = (field: keyof AutomationSettings) =>
    useMutation<AutomationSettings, ChatStatusTagsApiError, boolean>({
      mutationFn: (value: boolean) =>
        chatStatusTagsApi.updateSettings({ [field]: value } as AutomationSettingsPatch),
      onSuccess: (fresh) => queryClient.setQueryData(SETTINGS_KEY, fresh),
    })

  const reconcilerMutation = useToggleMutation('reconcilerEnabled')
  const autoResumeMutation = useToggleMutation('autoResumeEnabled')

  // A scheduler-unavailable 503 only arises on the reconciler write; render its
  // dedicated copy there, and the server's message everywhere else.
  const errorText = (m: typeof reconcilerMutation): string | null => {
    if (!m.isError || !m.error) return null
    return m.error.status === 503
      ? i18nT('apps.chatStatusTags.page.scheduler_unavailable')
      : i18nT('apps.chatStatusTags.page.toggle_error')
  }

  if (query.isLoading) return <ContentSkeleton rows={3} />
  // The reconcile-prompt card above already renders the 403/disabled and generic
  // load-failure states for the page; if settings cannot load, stay quiet rather
  // than double-reporting the same failure.
  if (query.isError || !query.data) return null

  const settings = query.data
  const reconcilerErr = errorText(reconcilerMutation)
  const autoResumeErr = errorText(autoResumeMutation)

  return (
    <Card className="mt-4" data-testid="cst-automation">
      <h2 className="text-sm font-semibold text-text-strong mb-3">
        {i18nT('apps.chatStatusTags.page.automation_heading')}
      </h2>

      {/* Reconciler switch. Toggle renders a role="switch" div (not a form
          control), so the label and help text sit beside it and are tied into
          the switch's accessible name/description rather than via <label>. */}
      <div className="flex items-start gap-2.5">
        <Toggle
          checked={settings.reconcilerEnabled}
          onChange={(v) => reconcilerMutation.mutate(v)}
          disabled={reconcilerMutation.isPending}
          tone="muted"
          label={i18nT('apps.chatStatusTags.page.reconciler_label')}
          describedBy="cst-reconciler-help"
        />
        <div className="min-w-0">
          <span className="text-[13px] text-text-strong">
            {i18nT('apps.chatStatusTags.page.reconciler_label')}
          </span>
          <p id="cst-reconciler-help" className="text-[12px] text-muted mt-0.5 max-w-[70ch]">
            {i18nT('apps.chatStatusTags.page.reconciler_help')}
          </p>
          {reconcilerErr ? (
            <span className="text-xs text-danger" data-testid="cst-reconciler-error">
              {reconcilerErr}
            </span>
          ) : null}
        </div>
      </div>

      <div className="flex items-start gap-2.5 mt-4">
        <Toggle
          checked={settings.autoResumeEnabled}
          onChange={(v) => autoResumeMutation.mutate(v)}
          disabled={autoResumeMutation.isPending}
          tone="muted"
          label={i18nT('apps.chatStatusTags.page.auto_resume_label')}
          describedBy="cst-auto-resume-help"
        />
        <div className="min-w-0">
          <span className="text-[13px] text-text-strong">
            {i18nT('apps.chatStatusTags.page.auto_resume_label')}
          </span>
          <p id="cst-auto-resume-help" className="text-[12px] text-muted mt-0.5 max-w-[70ch]">
            {i18nT('apps.chatStatusTags.page.auto_resume_help')}
          </p>
          {autoResumeErr ? (
            <span className="text-xs text-danger" data-testid="cst-auto-resume-error">
              {autoResumeErr}
            </span>
          ) : null}
        </div>
      </div>
    </Card>
  )
}

export default function ChatStatusTagsPage() {
  const queryClient = useQueryClient()

  const query = useQuery<ReconcilePrompt, ChatStatusTagsApiError>({
    queryKey: QUERY_KEY,
    queryFn: () => chatStatusTagsApi.reconcilePrompt(),
  })

  // The textarea's working copy. Seeded from the server prompt on load and after
  // every successful write, so "unchanged" is measured against what is actually
  // stored, not against the last thing the user typed.
  const [draft, setDraft] = useState<string>('')
  // Two-click confirm for reset: the first click arms, the second performs. No
  // modal — the destructive action is a single field's worth of text, and arming
  // is cleared by any edit or a successful save.
  const [resetArmed, setResetArmed] = useState(false)

  const serverPrompt = query.data?.prompt ?? ''
  useEffect(() => {
    if (query.data) setDraft(query.data.prompt)
  }, [query.data])

  const saveMutation = useMutation<ReconcilePrompt, ChatStatusTagsApiError, string>({
    mutationFn: (prompt: string) => chatStatusTagsApi.setReconcilePrompt(prompt),
    onSuccess: (data) => {
      // Write the server's canonical result straight into the cache so the
      // effect above re-seeds the draft and `isDefault` updates in one beat,
      // without a refetch round-trip.
      queryClient.setQueryData(QUERY_KEY, data)
      setResetArmed(false)
    },
  })

  const isDefault = query.data?.isDefault ?? false
  const dirty = draft !== serverPrompt
  const saving = saveMutation.isPending
  // A save that would send an empty string is really a reset; the explicit Reset
  // control owns that path, so the Save button stays disabled on an empty draft.
  const canSave = dirty && draft.trim().length > 0 && !saving

  // The scheduler state of the cron that actually runs the prompt above. Repair
  // re-registers (or unpauses) it and returns the resulting state, which we write
  // straight into the query cache so the row updates without a refetch.
  const cron = query.data?.cron
  const repairMutation = useMutation<{ ok: boolean; cron: ReconcileCron }, ChatStatusTagsApiError>({
    mutationFn: () => chatStatusTagsApi.repairCron(),
    onSuccess: ({ cron: repaired }) => {
      queryClient.setQueryData<ReconcilePrompt>(QUERY_KEY, (prev) =>
        prev ? { ...prev, cron: repaired } : prev,
      )
    },
  })
  const repairing = repairMutation.isPending

  const onEdit = (value: string) => {
    setDraft(value)
    if (resetArmed) setResetArmed(false)
  }

  const onReset = () => {
    if (!resetArmed) {
      setResetArmed(true)
      return
    }
    // Empty string == reset to default, per the contract. The response carries
    // the default text back, which re-seeds the draft.
    saveMutation.mutate('')
  }

  return (
    <>
      <PageHeader
        title={i18nT('apps.chatStatusTags.page.title')}
        subtitle={i18nT('apps.chatStatusTags.page.subtitle')}
      />

      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <Card>
          <p className="text-[13px] text-muted mb-3 max-w-[70ch]">
            {i18nT('apps.chatStatusTags.page.explanation')}
          </p>

          {query.isLoading ? (
            <ContentSkeleton rows={6} />
          ) : query.isError ? (
            query.error.status === 403 ? (
              <p className="text-[13px] text-warn" data-testid="cst-disabled">
                {i18nT('apps.chatStatusTags.page.disabled')}
              </p>
            ) : (
              <p className="text-[13px] text-danger" data-testid="cst-error">
                {query.error.message}
              </p>
            )
          ) : (
            <>
              <div className="flex items-center gap-2 mb-2">
                <label
                  htmlFor="cst-reconcile-prompt"
                  className="text-sm font-semibold text-text-strong"
                >
                  {i18nT('apps.chatStatusTags.page.prompt_label')}
                </label>
                {isDefault ? (
                  <Badge
                    variant="muted"
                    data-testid="cst-default-badge"
                    title={i18nT('apps.chatStatusTags.page.default_badge_title')}
                  >
                    <ShieldCheck className="lucide-inline" />{' '}
                    {i18nT('apps.chatStatusTags.page.default_badge')}
                  </Badge>
                ) : null}
              </div>

              <textarea
                id="cst-reconcile-prompt"
                data-testid="cst-reconcile-prompt"
                value={draft}
                onChange={(e) => onEdit(e.target.value)}
                spellCheck={false}
                aria-label={i18nT('apps.chatStatusTags.page.prompt_label')}
                className="w-full min-h-[22rem] resize-y bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-[13px] font-mono leading-relaxed outline-none transition-colors focus-ring"
              />

              <div className="flex items-center gap-2 flex-wrap mt-3">
                <Btn
                  primary
                  disabled={!canSave}
                  onClick={() => saveMutation.mutate(draft)}
                  data-testid="cst-save"
                >
                  <Save className="lucide-inline" />{' '}
                  {saving
                    ? i18nT('apps.chatStatusTags.page.saving')
                    : i18nT('apps.chatStatusTags.page.save')}
                </Btn>

                {/* Two-click reset: the first click swaps the label to a confirm,
                    the second sends the empty-string reset. Disabled when the
                    stored prompt is already the default — there is nothing to
                    restore. */}
                <Btn
                  danger
                  disabled={isDefault || saving}
                  onClick={onReset}
                  data-testid="cst-reset"
                  title={i18nT('apps.chatStatusTags.page.reset_title')}
                >
                  <RotateCcw className="lucide-inline" />{' '}
                  {resetArmed
                    ? i18nT('apps.chatStatusTags.page.reset_confirm')
                    : i18nT('apps.chatStatusTags.page.reset')}
                </Btn>

                {dirty && !saving ? (
                  <span className="text-xs text-muted" data-testid="cst-unsaved">
                    {i18nT('apps.chatStatusTags.page.unsaved_changes')}
                  </span>
                ) : null}

                {saveMutation.isError ? (
                  <span className="text-xs text-danger" data-testid="cst-save-error">
                    {saveMutation.error.message}
                  </span>
                ) : null}
              </div>

              {/* Schedule status for the cron that actually runs this prompt.
                  Healthy: a muted one-liner. Missing / paused: a warning plus a
                  Repair button that re-registers (or unpauses) the job. When the
                  scheduler itself is unreachable we can neither confirm the state
                  nor repair, so the button is shown disabled. */}
              {cron ? (
                <div
                  className="flex items-center gap-2 flex-wrap mt-4 pt-3 border-t border-border"
                  data-testid="cst-cron-row"
                >
                  {cron.present && cron.enabled ? (
                    <span className="text-xs text-muted inline-flex items-center gap-1.5" data-testid="cst-cron-ok">
                      <CalendarClock className="lucide-inline" />{' '}
                      {i18nT('apps.chatStatusTags.page.cron_ok', { schedule: cron.schedule })}
                    </span>
                  ) : (
                    <>
                      <span className="text-xs text-warn inline-flex items-center gap-1.5" data-testid="cst-cron-warn">
                        <AlertTriangle className="lucide-inline" />{' '}
                        {i18nT(
                          cron.schedulerUnavailable || !cron.present
                            ? 'apps.chatStatusTags.page.cron_missing'
                            : 'apps.chatStatusTags.page.cron_disabled',
                        )}
                      </span>
                      <Btn
                        disabled={repairing || cron.schedulerUnavailable}
                        onClick={() => repairMutation.mutate()}
                        data-testid="cst-cron-repair"
                      >
                        <Wrench className="lucide-inline" />{' '}
                        {repairing
                          ? i18nT('apps.chatStatusTags.page.cron_repairing')
                          : i18nT('apps.chatStatusTags.page.cron_repair')}
                      </Btn>
                      {repairMutation.isError ? (
                        <span className="text-xs text-danger" data-testid="cst-cron-error">
                          {repairMutation.error.message}
                        </span>
                      ) : null}
                    </>
                  )}
                </div>
              ) : null}
            </>
          )}
        </Card>

        <AutomationSection />
      </div>
    </>
  )
}
