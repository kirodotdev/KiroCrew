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
import ErrorNotice from '../../../components/ErrorNotice'
import { SecretField } from '../../../components/SecretField'
import { meetingsApi, type CalendarCredentialsResponse, type CredentialStatus } from '../api'

interface Props {
  provider: string
  providerLabel: string
  notify: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
}

const CREDENTIALS_QUERY_KEY = ['meetings', 'calendar', 'credentials'] as const

/**
 * Where a provider's OAuth client id and secret come from. Both fields are
 * created by the user in the provider's own console; the form cannot help with
 * that step, so it at least says where it happens.
 */
const SETUP_LINKS: Record<string, string> = {
  google: 'https://console.cloud.google.com/apis/credentials',
  microsoft: 'https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade',
}

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

export type CredentialBadge = 'connected' | 'saved' | 'partial' | 'none'

/**
 * What the status badge says, from field NAMES alone.
 *
 * Only an OAuth provider can be `connected`: its refresh token exists because a
 * sign-in against the real tenant succeeded. A password provider is never
 * claimed connected -- nothing here has spoken to the server, so a mistyped
 * password is indistinguishable from a right one until the next sync. With every
 * field stored it reads `saved`; with some of them, `partial`.
 */
export function credentialBadge(
  schema: { fields: string[]; oauth: boolean },
  stored: string[],
): CredentialBadge {
  if (stored.length === 0) return 'none'
  if (schema.oauth) return stored.includes('refresh_token') ? 'connected' : 'partial'
  return schema.fields.every(field => stored.includes(field)) ? 'saved' : 'partial'
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
      // must not replace the dashboard tab the user is authenticated in. The
      // return value is NOT consulted: with `noopener` the spec answers null
      // whether or not a tab opened, so a blocked popup cannot be told from an
      // opened one. The link below is therefore always offered, and the toast is
      // worded for both outcomes.
      window.open(response.authorize_url, '_blank', 'noopener,noreferrer')
      setAuthorizeUrl(response.authorize_url)
      notify(i18nT('apps.meetings.settings.connectStarted'), { type: 'info' })
    },
    onError: (error: Error) =>
      notify(error.message || i18nT('apps.meetings.settings.connectFailed'), { type: 'error' }),
  })

  const loadFailed = query.isError
    ? i18nT('apps.meetings.settings.credentialsUnavailable')
    : null

  if (!schema) {
    // A load that failed must not look like "this provider needs nothing": with
    // no form on screen there is no draft to lose, so the hand-off is on.
    if (loadFailed) {
      return (
        <div className="mt-3" data-testid="calendar-credentials-error">
          <ErrorNotice message={loadFailed} variant="inline" askAgent />
        </div>
      )
    }
    return null
  }

  const pending: Record<string, string | null> = {}
  for (const field of schema.fields) {
    if (cleared[field]) pending[field] = null
    else if ((drafts[field] ?? '') !== '') pending[field] = drafts[field]
  }
  const hasPending = Object.keys(pending).length > 0
  const badge = credentialBadge(schema, stored)
  const connected = badge === 'connected'
  const busy = save.isPending || forget.isPending || connect.isPending
  const canSignIn = schema.oauth && stored.includes('client_id') && !hasPending
  const setupLink = schema.oauth && SETUP_LINKS[provider] ? { href: SETUP_LINKS[provider] } : undefined
  // The most recent failure, kept on screen until the next attempt succeeds. The
  // toasts above are transient; this is where a save that did not persist stays
  // visible. Exactly one is shown, because they share the row.
  const actionFailed = save.isError
    ? save.error.message || i18nT('apps.meetings.settings.credentialsSaveFailed')
    : forget.isError
      ? forget.error.message || i18nT('apps.meetings.settings.credentialsForgetFailed')
      : connect.isError
        ? connect.error.message || i18nT('apps.meetings.settings.connectFailed')
        : null

  return (
    <div className="mt-3 flex flex-col gap-2" data-testid="calendar-credentials">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[13px] font-semibold text-text">{providerLabel}</span>
        {badge === 'connected' ? (
          <Badge variant="ok">{i18nT('apps.meetings.settings.statusConnected')}</Badge>
        ) : badge === 'saved' ? (
          <Badge variant="ok">{i18nT('apps.meetings.settings.statusCredentialsSaved')}</Badge>
        ) : badge === 'partial' ? (
          <Badge variant="warn">{i18nT('apps.meetings.settings.statusNotConnected')}</Badge>
        ) : (
          <Badge variant="muted">{i18nT('apps.meetings.settings.statusNotConnected')}</Badge>
        )}
        {/* Disconnect lives on the status row, not beside Save / Sign in: it undoes
            what the badge reports, and the action row stays at two buttons. */}
        {stored.length > 0 && (
          <Btn
            danger
            className="ml-auto"
            disabled={busy}
            onClick={() => forget.mutate()}
            aria-label={i18nT('apps.meetings.settings.disconnectCalendar')}
          >
            <Unplug className="lucide-inline" />
            {i18nT('apps.meetings.settings.disconnectCalendar')}
          </Btn>
        )}
      </div>
      <p className="text-[12px] text-muted">{i18nT('apps.meetings.settings.credentialsHelp')}</p>
      {/* No hand-off: the fields below hold credential values typed but not yet
          saved, and the hand-off unmounts this tree. */}
      <ErrorNotice message={loadFailed ?? actionFailed} variant="inline" />
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
          setupLink={setupLink}
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
