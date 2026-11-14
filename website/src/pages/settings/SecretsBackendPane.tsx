import { useEffect, useRef, useState } from 'react'
import { ExternalLink, Vault } from 'lucide-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import ErrorNotice from '../../components/ErrorNotice'
import InfoTip from '../../components/InfoTip'
import { Btn, Card, Input } from '../../components/ui'
import { api, type SecretBackendError, type SecretBackendView } from '../../api/client'
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
}

/** Codes meaning this install cannot run the backend at all: one sentence of
 *  their own, never framed as a failed action, because nothing was tried. */
const NOT_INSTALLED: ReadonlySet<string> = new Set(['sdk_missing', 'sdk_unsupported'])

/** The sentence for a backend failure, in the reader's language. */
export function backendErrorText(name: string, error: SecretBackendError): string {
  if (error.message_key) return i18nT(error.message_key, { ...error.params, name })
  const key = GENERIC_ERROR_KEYS[error.code]
  return key ? i18nT(key, { ...error.params, name }) : error.message
}

/** The status word for a backend's row and pane header. */
export function backendStatusText(view: SecretBackendView): string {
  if (view.status === 'unavailable') return i18nT('settings.secrets.backend_unavailable')
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
function FailureNotice({ name, failure, frameKey, askAgent }: { name: string; failure: SecretBackendError; frameKey: string; askAgent: boolean }) {
  const text = backendErrorText(name, failure)
  return (
    <div className="space-y-1.5">
      <ErrorNotice variant="inline" askAgent={askAgent} message={NOT_INSTALLED.has(failure.code) ? text : i18nT(frameKey, { error: text, name })} />
      {failure.install_command && (
        <code className="block overflow-x-auto rounded-md border border-border bg-bg-elevated px-2.5 py-1.5 font-mono text-[12px] text-accent">
          {failure.install_command}
        </code>
      )}
    </div>
  )
}

/** Settings → Secrets → one password manager: its connect form, as the backend
 *  declares it, or its status and Disconnect. The same component for every
 *  backend; nothing here names one. Read-only: nothing at the provider changes. */
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
  const hasDraft = Object.values(values).some(value => value !== '')
  const draftRef = useRef(onDraftChange)
  draftRef.current = onDraftChange
  useEffect(() => {
    draftRef.current(hasDraft)
  }, [hasDraft])
  useEffect(() => () => draftRef.current(false), [])
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['secrets'] })
  const connect = useMutation({
    mutationFn: () => api.secretsBackendConnect(view.id, values),
    onSuccess: async () => {
      // A secret field never stays in memory past a successful connect.
      setValues({})
      await refresh()
    },
  })
  const disconnect = useMutation({
    mutationFn: () => api.secretsBackendDisconnect(view.id),
    onSuccess: refresh,
  })
  const failure = connect.isError ? errorBody(connect.error) : null
  const ready = view.form.every(field => !field.required || (values[field.name] ?? '').trim())
  // Not connected and not installable here: there is nothing to fill in.
  const showForm = !view.connected && view.status !== 'unavailable'
  // The help link answers "where does this come from?" for the first secret field.
  const tokenIndex = view.form.findIndex(field => field.secret)

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
        {view.connected && (
          <Btn onClick={() => disconnect.mutate()} disabled={disconnect.isPending}>
            {i18nT('settings.secrets.backend_disconnect')}
          </Btn>
        )}
      </div>

      {view.error && (
        <FailureNotice name={view.name} failure={view.error} frameKey="settings.secrets.backend_list_error" askAgent={allowAgentHandoff} />
      )}
      {disconnect.isError && (
        <ErrorNotice variant="inline" askAgent={allowAgentHandoff} message={i18nT('settings.secrets.backend_disconnect_error', { error: errorBody(disconnect.error).message })} />
      )}

      {showForm && (
        <Card className="mb-0 py-3.5">
          <form
            className="space-y-3"
            onSubmit={e => {
              e.preventDefault()
              if (ready) connect.mutate()
            }}
          >
            {view.form.map((field, index) => (
              <label key={field.name} className="block">
                <span className="mb-1 flex max-w-md flex-wrap items-baseline justify-between gap-x-3 text-[13px] font-medium text-muted">
                  <span className="inline-flex items-center gap-1.5">
                    {i18nT(field.label_key)}
                    {!field.required && <span className="font-normal">{i18nT('settings.secrets.backend_optional')}</span>}
                    {field.hint_key && <InfoTip text={i18nT(field.hint_key)} placement="top" />}
                  </span>
                  {/* Where the token comes from sits on the token's own label row. */}
                  {index === tokenIndex && view.help_url && view.help_label_key && (
                    <a href={view.help_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 font-normal text-accent hover:underline">
                      {i18nT(view.help_label_key)}
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
                  disabled={connect.isPending}
                />
              </label>
            ))}
            {/* No hand-off: the connect form holds the unsaved token and fields; a hand-off would navigate away and discard them. */}
            {failure && (
              <FailureNotice name={view.name} failure={failure} frameKey="settings.secrets.backend_connect_error" askAgent={false} />
            )}
            <div className="flex flex-wrap items-center gap-2">
              <Btn primary type="submit" busy={connect.isPending} disabled={!ready || connect.isPending}>
                {i18nT('settings.secrets.backend_connect')}
              </Btn>
            </div>
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
