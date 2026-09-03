// The credential half of Settings -> Calendar: what a provider needs before it
// can be read (a CalDAV username/password, or an OAuth client id plus a browser
// sign-in), whether that is in place, and the buttons to save, sign in, or
// disconnect.
//
// Two backend facts shape this component:
//
//   * A stored value never comes back. `GET /calendar/credentials` answers field
//     NAMES and booleans, so every field renders through `SecretField`'s
//     write-only state — a set field shows a mask and Replace/Remove, never the
//     value — and "connected" is derived from which names are present.
//   * The form is the backend's allowlist. `providers` in the same response is
//     built from the exact table the PUT is checked against, so the fields shown
//     here are the fields that can be written and nothing is hardcoded per
//     provider; a provider missing from it takes no credentials and renders
//     nothing.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, LogIn, Unplug } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { Badge, Btn } from '../../../components/ui'
import { SecretField } from '../../../components/SecretField'
import { meetingsApi, type CalendarCredentialsResponse, type CredentialStatus } from '../api'

interface Props {
  provider: string
  providerLabel: string
  notify: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
}

export const CREDENTIALS_QUERY_KEY = ['meetings', 'calendar', 'credentials'] as const

/** Full literal keys per field, never assembled: the i18n gate reads them. */
function fieldLabel(name: string): string {
  switch (name) {
    case 'username':
      return i18nT('apps.meetings.settings.fieldUsername')
    case 'password':
      return i18nT('apps.meetings.settings.fieldPassword')
    case 'client_id':
      return i18nT('apps.meetings.settings.fieldClientId')
    case 'client_secret':
      return i18nT('apps.meetings.settings.fieldClientSecret')
    default:
      return name
  }
}

/** A stored secret is shown as a fixed mask: no preview text exists for it to reveal. */
const MASK = '••••••••'

/** OAuth is connected once a refresh token is stored; a password provider once every field is. */
export function isConnected(schema: { fields: string[]; oauth: boolean }, stored: string[]): boolean {
  if (schema.oauth) return stored.includes('refresh_token')
  return schema.fields.every(field => stored.includes(field))
}

export default function CalendarCredentials({ provider, providerLabel, notify }: Props) {
  const queryClient = useQueryClient()
  // Fresh on every focus: the OAuth consent finishes in ANOTHER tab, and the
  // user comes back here expecting the badge to have flipped to Connected.
  const query = useQuery({
    queryKey: CREDENTIALS_QUERY_KEY,
    queryFn: meetingsApi.calendarCredentials,
    staleTime: 0,
    refetchOnWindowFocus: true,
  })
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [cleared, setCleared] = useState<Record<string, boolean>>({})
  const [authorizeUrl, setAuthorizeUrl] = useState<string | null>(null)

  const schema = query.data?.providers[provider]
  const stored = query.data?.status[provider]?.fields ?? []

  const applyStatus = (status: Record<string, CredentialStatus>) => {
    queryClient.setQueryData<CalendarCredentialsResponse>(CREDENTIALS_QUERY_KEY, previous =>
      previous ? { ...previous, status } : previous,
    )
  }

  const save = useMutation({
    mutationFn: (values: Record<string, string | null>) =>
      meetingsApi.saveCalendarCredentials(provider, values),
    onSuccess: response => {
      applyStatus(response.status)
      setDrafts({})
      setCleared({})
      notify(i18nT('apps.meetings.settings.credentialsSaved'), { type: 'success' })
    },
    onError: (error: Error) =>
      notify(error.message || i18nT('apps.meetings.settings.credentialsSaveFailed'), {
        type: 'error',
      }),
  })

  const forget = useMutation({
    mutationFn: () => meetingsApi.forgetCalendarCredentials(provider),
    onSuccess: response => {
      applyStatus(response.status)
      setDrafts({})
      setCleared({})
      setAuthorizeUrl(null)
      notify(i18nT('apps.meetings.settings.credentialsForgot'), { type: 'success' })
    },
    onError: (error: Error) =>
      notify(error.message || i18nT('apps.meetings.settings.credentialsForgetFailed'), {
        type: 'error',
      }),
  })

  const connect = useMutation({
    mutationFn: () => meetingsApi.startCalendarOAuth(provider),
    onSuccess: response => {
      // `window.open`, not `location.href`: inside the Electron shell the window
      // handler forwards it to the OS browser, and in a browser the consent page
      // must not replace the dashboard tab the user is authenticated in. A popup
      // blocker answers null; the link below is the fallback for that case.
      const opened = window.open(response.authorize_url, '_blank', 'noopener,noreferrer')
      setAuthorizeUrl(opened ? null : response.authorize_url)
      notify(i18nT('apps.meetings.settings.connectStarted'), { type: 'info' })
    },
    onError: (error: Error) =>
      notify(error.message || i18nT('apps.meetings.settings.connectFailed'), { type: 'error' }),
  })

  if (!schema) return null

  const pending: Record<string, string | null> = {}
  for (const field of schema.fields) {
    if (cleared[field]) pending[field] = null
    else if ((drafts[field] ?? '') !== '') pending[field] = drafts[field]
  }
  const hasPending = Object.keys(pending).length > 0
  const connected = isConnected(schema, stored)
  const busy = save.isPending || forget.isPending || connect.isPending
  const canSignIn = schema.oauth && stored.includes('client_id') && !hasPending

  return (
    <div className="mt-3 flex flex-col gap-2" data-testid="calendar-credentials">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[13px] font-semibold text-text">{providerLabel}</span>
        {connected ? (
          <Badge variant="ok">{i18nT('apps.meetings.settings.statusConnected')}</Badge>
        ) : stored.length > 0 ? (
          <Badge variant="warn">{i18nT('apps.meetings.settings.statusCredentialsSaved')}</Badge>
        ) : (
          <Badge variant="muted">{i18nT('apps.meetings.settings.statusNotConnected')}</Badge>
        )}
      </div>
      <p className="text-[12px] text-muted">{i18nT('apps.meetings.settings.credentialsHelp')}</p>
      {query.isError && (
        <p className="text-[12px] text-danger">
          {i18nT('apps.meetings.settings.credentialsUnavailable')}
        </p>
      )}
      {schema.fields.map(field => (
        <SecretField
          key={field}
          label={fieldLabel(field)}
          placeholder={i18nT('apps.meetings.settings.credentialPlaceholder')}
          isSet={stored.includes(field)}
          preview={MASK}
          value={drafts[field] ?? ''}
          onChange={value => setDrafts(current => ({ ...current, [field]: value }))}
          cleared={cleared[field] ?? false}
          onClearedChange={flag => setCleared(current => ({ ...current, [field]: flag }))}
        />
      ))}
      <div className="flex items-center gap-2 flex-wrap">
        <Btn
          primary
          disabled={!hasPending || busy}
          onClick={() => save.mutate(pending)}
          aria-label={i18nT('apps.meetings.settings.saveCredentials')}
        >
          {i18nT('apps.meetings.settings.saveCredentials')}
        </Btn>
        {schema.oauth && (
          <Btn
            disabled={!canSignIn || busy}
            onClick={() => connect.mutate()}
            aria-label={i18nT('apps.meetings.settings.connectCalendar', { provider: providerLabel })}
            title={
              canSignIn ? undefined : i18nT('apps.meetings.settings.connectNeedsClientId')
            }
          >
            <LogIn className="lucide-inline" />
            {i18nT('apps.meetings.settings.connectCalendar', { provider: providerLabel })}
          </Btn>
        )}
        {stored.length > 0 && (
          <Btn
            danger
            disabled={busy}
            onClick={() => forget.mutate()}
            aria-label={i18nT('apps.meetings.settings.disconnectCalendar')}
          >
            <Unplug className="lucide-inline" />
            {i18nT('apps.meetings.settings.disconnectCalendar')}
          </Btn>
        )}
      </div>
      {schema.oauth && !canSignIn && !connected && !hasPending && (
        <p className="text-[12px] text-muted">
          {i18nT('apps.meetings.settings.connectNeedsClientId')}
        </p>
      )}
      {authorizeUrl && (
        <a
          href={authorizeUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-[13px] text-accent hover:underline"
        >
          <ExternalLink className="lucide-inline" />
          {i18nT('apps.meetings.settings.connectOpenLink')}
        </a>
      )}
    </div>
  )
}
