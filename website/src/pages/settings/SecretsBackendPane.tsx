import { useEffect, useRef, useState } from 'react'
import { ExternalLink, Loader2, Vault } from 'lucide-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import ErrorNotice from '../../components/ErrorNotice'
import StatusNotice from '../../components/StatusNotice'
import { CopyCommandButton } from '../../components/settingRef/CopyCommandButton'
import { Btn, Card, Input } from '../../components/ui'
import { api, type SecretBackendError, type SecretBackendFormField, type SecretBackendView } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { SECRETS_GUIDE_URL } from '../../utils/docsLink'

/** The codes the core words the same way for every backend. A backend's own
 *  code arrives with its `message_key`; anything else shows the gateway's
 *  message inside the action's frame. */
const GENERIC_ERROR_KEYS: Readonly<Record<string, string>> = {
  sdk_missing: 'settings.secrets.backend_err_sdk_missing',
  sdk_unsupported: 'settings.secrets.backend_err_sdk_unsupported',
  unreachable: 'settings.secrets.backend_err_unreachable',
  unauthorized: 'settings.secrets.backend_err_unauthorized',
  rate_limited: 'settings.secrets.backend_err_rate_limited',
  timeout: 'settings.secrets.backend_err_timeout',
  invalid_input: 'settings.secrets.backend_err_invalid_input',
  ambiguous: 'settings.secrets.backend_err_ambiguous',
  connection_unreadable: 'settings.secrets.backend_err_connection_unreadable',
  app_missing: 'settings.secrets.backend_app_missing',
  not_approved: 'settings.secrets.backend_err_not_approved',
  disconnected: 'settings.secrets.backend_err_disconnected',
  replaced: 'settings.secrets.backend_err_replaced',
  approval_expired: 'settings.secrets.backend_approve_lead',
}

/** Codes meaning this install cannot run the backend at all: one sentence of
 *  their own, never framed as a failed action, because nothing was tried. */
const NOT_INSTALLED: ReadonlySet<string> = new Set(['sdk_missing', 'sdk_unsupported'])
/** Failures whose text says to try again: the pane offers the button for it. */
const RETRYABLE: ReadonlySet<string> = new Set(['unreachable', 'timeout', 'rate_limited'])

/** The sentence for a backend failure, in the reader's language. */
export function backendErrorText(name: string, error: SecretBackendError): string {
  if (error.message_key) return i18nT(error.message_key, { ...error.params, name })
  const key = GENERIC_ERROR_KEYS[error.code]
  return key ? i18nT(key, { ...error.params, name }) : error.message
}

/** The status word for a backend's row and pane header. */
export function backendStatusText(view: SecretBackendView): string {
  if (view.status === 'unavailable') return i18nT('settings.secrets.backend_unavailable')
  if (view.status === 'needs_approval') return i18nT('settings.secrets.backend_needs_approval')
  if (view.connected) return view.account ?? ''
  return i18nT('settings.secrets.backend_not_connected')
}

function errorBody(error: unknown): SecretBackendError {
  const raw = (error as { body?: unknown })?.body
  try {
    const parsed = typeof raw === 'string' && raw ? JSON.parse(raw) : null
    if (parsed && typeof parsed === 'object') {
      return {
        code: String(parsed.code ?? ''),
        message: String(parsed.error ?? (error as Error).message),
        message_key: parsed.message_key,
        params: parsed.params,
        install_command: parsed.install_command,
      }
    }
  } catch {
    // Not JSON: the transport's own message below.
  }
  return { code: '', message: (error as Error)?.message ?? String(error) }
}

/** One failure line, plus the install command where the gateway can add a
 *  missing package to its own interpreter (the shape of a missing voice extra). */
function FailureNotice({
  name,
  failure,
  frameKey,
  askAgent,
  onRetry,
}: {
  name: string
  failure: SecretBackendError
  frameKey: string
  askAgent: boolean
  /** Offered for a failure that says "try again" (the provider was down or slow). */
  onRetry?: () => void
}) {
  const text = backendErrorText(name, failure)
  return (
    <div className="space-y-1.5">
      <ErrorNotice variant="inline" askAgent={askAgent} actionPlacement="below-on-phone" message={NOT_INSTALLED.has(failure.code) ? text : i18nT(frameKey, { error: text, name })} />
      {onRetry && RETRYABLE.has(failure.code) && (
        <div>
          <Btn onClick={onRetry}>{i18nT('components.errorBoundary.try_again')}</Btn>
        </div>
      )}
      {failure.install_command && <div className="text-[13px] text-muted">{i18nT('settings.secrets.backend_install_lead')}</div>}
      {failure.install_command && (
        <div className="flex items-start gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2">
          <code className="flex-1 break-all font-mono text-[12px] text-text">{failure.install_command}</code>
          <CopyCommandButton text={failure.install_command} />
        </div>
      )}
    </div>
  )
}

/** The inputs of one declared form, labelled by the backend's own keys. */
function FormFields({
  fields,
  values,
  setValues,
  disabled,
  helpUrl,
  helpLabelKey,
  hideLabels = false,
}: {
  fields: SecretBackendFormField[]
  values: Record<string, string>
  setValues: (update: (current: Record<string, string>) => Record<string, string>) => void
  disabled: boolean
  helpUrl?: string
  helpLabelKey?: string
  /** The step above already says what goes in; the label stays for screen readers. */
  hideLabels?: boolean
}) {
  // The help link answers "where does this come from?" for the first secret field.
  const tokenIndex = fields.findIndex(field => field.secret)
  return (
    <>
      {fields.map((field, index) => (
        <label key={field.name} className="block">
          <span className={hideLabels ? 'sr-only' : 'mb-1 flex max-w-md flex-wrap items-baseline justify-between gap-x-3 text-[13px] font-medium text-muted'}>
            <span className="inline-flex items-center gap-1.5">
              {i18nT(field.label_key)}
              {!field.required && <span className="font-normal">{i18nT('settings.secrets.backend_optional')}</span>}
            </span>
            {/* Where the token comes from sits on the token's own label row. */}
            {!hideLabels && index === tokenIndex && helpUrl && helpLabelKey && (
              <a href={helpUrl} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 font-normal text-accent hover:underline">
                {i18nT(helpLabelKey)}
                <ExternalLink className="lucide-inline" aria-hidden />
              </a>
            )}
          </span>
          <Input
            type={field.secret ? 'password' : 'text'}
            value={values[field.name] ?? ''}
            onChange={e => setValues(current => ({ ...current, [field.name]: e.target.value }))}
            placeholder={field.placeholder_key ? i18nT(field.placeholder_key) : undefined}
            className={`w-full max-w-md${field.secret ? ' font-mono' : ''}`}
            autoComplete="off"
            required={field.required}
            disabled={disabled}
          />
        </label>
      ))}
    </>
  )
}

/** "Click Approve in 1Password", while a sign-in waits on the provider's app. */
function WaitingForApp({ name }: { name: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-[14px] font-medium text-text-strong">
      <Loader2 className="h-4 w-4 animate-spin text-accent" aria-hidden />
      {i18nT('settings.secrets.backend_app_waiting', { name })}
    </span>
  )
}

function StepBadge({ n }: { n: number }) {
  return (
    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent-subtle text-[12px] font-semibold text-accent" aria-hidden>
      {n}
    </span>
  )
}

/** Settings → Secrets → one password manager: its connect form, as the backend
 *  declares it, or its status and Disconnect. The same component for every
 *  backend; nothing here names one. Read-only: nothing at the provider changes.
 *
 *  A backend with a fallback form (a token) opens on its main form unless the
 *  main way cannot work here (`fallback_first`, or a connect that answered
 *  `app_missing`); the person can switch either way. */
export function SecretsBackendPane({
  view,
  allowAgentHandoff,
  onDraftChange,
}: {
  view: SecretBackendView
  allowAgentHandoff: boolean
  /** Tells the panel an unsaved connect form is open, so no hand-off leaves it. */
  onDraftChange: (hasDraft: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [values, setValues] = useState<Record<string, string>>({})
  const [chosenFallback, setChosenFallback] = useState(false)
  const hasDraft = Object.values(values).some(value => value !== '')
  const draftRef = useRef(onDraftChange)
  draftRef.current = onDraftChange
  useEffect(() => {
    draftRef.current(hasDraft)
  }, [hasDraft])
  useEffect(() => () => draftRef.current(false), [])
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['secrets'] })
  const hasFallback = view.fallback_form.length > 0
  const [appMissing, setAppMissing] = useState(false)
  const onFallback = hasFallback && (chosenFallback || appMissing || view.fallback_first)
  const fields = onFallback ? view.fallback_form : view.form
  const connect = useMutation({
    mutationFn: () => api.secretsBackendConnect(view.id, onFallback ? { form: 'fallback', ...values } : values),
    onSuccess: async () => {
      // A secret field never stays in memory past a successful connect.
      setValues({})
      setChosenFallback(false)
      await refresh()
    },
    onError: error => {
      // The app is not on this computer: offer the token way at once.
      if (hasFallback && errorBody(error).code === 'app_missing') {
        setAppMissing(true)
        setValues({})
      }
    },
  })
  const approve = useMutation({
    mutationFn: () => api.secretsBackendApprove(view.id),
    onSuccess: refresh,
  })
  const disconnect = useMutation({
    mutationFn: () => api.secretsBackendDisconnect(view.id),
    onSuccess: refresh,
  })
  const failure = connect.isError ? errorBody(connect.error) : null
  const shownFailure = failure
  const ready = fields.every(field => !field.required || (values[field.name] ?? '').trim())
  // Not connected and not installable here: there is nothing to fill in.
  const showForm = !view.connected && view.status !== 'unavailable'
  const needsApproval = view.status === 'needs_approval'
  // Signed in through the app: a token can replace it, for jobs run while away.
  const appConnected = view.connected && view.app_sign_in && view.connected_form === 'main'
  const showTokenForm = hasFallback && ((showForm && onFallback) || (appConnected && chosenFallback))
  const switchForm = (fallback: boolean) => {
    setChosenFallback(fallback)
    setAppMissing(false)
    setValues({})
    connect.reset()
  }
  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (ready) connect.mutate()
  }
  const waiting = connect.isPending && view.app_sign_in && !onFallback

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="flex items-start gap-3">
        <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-accent-subtle text-accent" aria-hidden>
          <Vault />
        </span>
        <div className="min-w-0 flex-1">
          <h4 className="truncate text-[17px] font-semibold text-text-strong">{view.name}</h4>
          <div className="mt-0.5 text-[12.5px] text-muted">{backendStatusText(view)}</div>
        </div>
        {/* Not while a sign-in waits on the app: its answer would land after the Disconnect. */}
        {view.connected && (
          <Btn onClick={() => disconnect.mutate()} disabled={disconnect.isPending || approve.isPending || connect.isPending}>
            {i18nT('settings.secrets.backend_disconnect')}
          </Btn>
        )}
      </div>

      {view.error && (
        <FailureNotice name={view.name} failure={view.error} frameKey="settings.secrets.backend_list_error" askAgent={allowAgentHandoff} onRetry={refresh} />
      )}
      {disconnect.isError && (
        <ErrorNotice variant="inline" askAgent={allowAgentHandoff} message={i18nT('settings.secrets.backend_disconnect_error', { error: errorBody(disconnect.error).message })} />
      )}

      {needsApproval && (
        <Card className="mb-0 py-3.5">
          <div className="space-y-3">
            {approve.isPending ? (
              <WaitingForApp name={view.name} />
            ) : (
              <Btn primary onClick={() => approve.mutate()} disabled={disconnect.isPending || connect.isPending}>
                {i18nT('settings.secrets.backend_approve_again')}
              </Btn>
            )}
            {approve.isError && (
              <FailureNotice name={view.name} failure={errorBody(approve.error)} frameKey="settings.secrets.backend_connect_error" askAgent={allowAgentHandoff} />
            )}
          </div>
        </Card>
      )}

      {/* Said once, where it matters: the app way needs someone there to approve. */}
      {appConnected && (
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-[13px] text-muted">
          <span>{i18nT('settings.secrets.backend_away_needs_token')}</span>
          {hasFallback && !chosenFallback && (
            <button type="button" className="text-accent hover:underline" onClick={() => switchForm(true)} disabled={disconnect.isPending || approve.isPending}>
              {i18nT(view.fallback_label_key)}
            </button>
          )}
        </div>
      )}

      {showForm && !onFallback && (
        <Card className="mb-0 py-3.5">
          <form className="space-y-3" onSubmit={submit}>
            <FormFields
              fields={view.form}
              values={values}
              setValues={setValues}
              disabled={connect.isPending}
              helpUrl={view.help_url}
              helpLabelKey={view.help_label_key}
            />
            {/* No hand-off: the connect form holds the unsaved values; a hand-off would navigate away and discard them. */}
            {shownFailure && (
              <FailureNotice name={view.name} failure={shownFailure} frameKey="settings.secrets.backend_connect_error" askAgent={false} />
            )}
            <div className="flex flex-wrap items-center gap-2">
              {waiting ? (
                <WaitingForApp name={view.name} />
              ) : (
                <Btn primary type="submit" busy={connect.isPending} disabled={!ready || connect.isPending}>
                  {view.app_sign_in ? i18nT('settings.secrets.backend_connect_app', { name: view.name }) : i18nT('settings.secrets.backend_connect')}
                </Btn>
              )}
            </div>
            {hasFallback && (
              <button type="button" className="text-[13px] text-accent hover:underline" onClick={() => switchForm(true)} disabled={connect.isPending}>
                {i18nT(view.fallback_label_key)}
              </button>
            )}
          </form>
        </Card>
      )}

      {showTokenForm && (
        <Card className="mb-0 py-3.5">
          <form className="space-y-4" onSubmit={submit}>
            {/* The preflight check only; a connect that answered app_missing shows its own notice below. */}
            {view.fallback_first && !appMissing && (
              <StatusNotice tone="info" message={i18nT('settings.secrets.backend_app_missing', { name: view.name })} />
            )}
            <ol className="space-y-4">
              {view.help_url && (
                <li className="flex gap-3">
                  <StepBadge n={1} />
                  <div className="min-w-0 flex-1 space-y-2">
                    <div className="text-[14px] font-medium text-text-strong">{i18nT('settings.secrets.backend_token_step_make', { name: view.name })}</div>
                    <a
                      href={view.help_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-border px-4 text-sm font-medium text-text hover:bg-bg-hover"
                    >
                      {i18nT('settings.secrets.backend_token_step_open', { name: view.name })}
                      <ExternalLink className="lucide-inline" aria-hidden />
                    </a>
                  </div>
                </li>
              )}
              <li className="flex gap-3">
                <StepBadge n={view.help_url ? 2 : 1} />
                <div className="min-w-0 flex-1 space-y-2">
                  <div className="text-[14px] font-medium text-text-strong">{i18nT('settings.secrets.backend_token_step_paste')}</div>
                  <FormFields fields={view.fallback_form} values={values} setValues={setValues} disabled={connect.isPending} hideLabels />
                  {/* No hand-off: the token step holds the unsaved pasted token; a hand-off would navigate away and discard it. */}
                  {shownFailure && (
                    <FailureNotice name={view.name} failure={shownFailure} frameKey="settings.secrets.backend_connect_error" askAgent={false} />
                  )}
                  <Btn primary type="submit" busy={connect.isPending} disabled={!ready || connect.isPending || approve.isPending}>
                    {i18nT('settings.secrets.backend_connect')}
                  </Btn>
                </div>
              </li>
            </ol>
            {!appMissing && !view.fallback_first && (
              <button type="button" className="text-[13px] text-accent hover:underline" onClick={() => switchForm(false)} disabled={connect.isPending}>
                {i18nT('settings.secrets.backend_use_app', { name: view.name })}
              </button>
            )}
          </form>
        </Card>
      )}

      {/* Wraps on a phone so the link drops below the sentence instead of breaking in two. */}
      <div className="mt-auto flex flex-wrap items-center justify-between gap-x-4 gap-y-1 pt-2 text-[12px] text-muted">
        <span>{i18nT('settings.secrets.backend_read_only', { name: view.name })}</span>
        <a href={SECRETS_GUIDE_URL} target="_blank" rel="noopener noreferrer" className="whitespace-nowrap text-accent hover:underline">
          {i18nT('settings.secrets.how_it_works')}
        </a>
      </div>
    </div>
  )
}
