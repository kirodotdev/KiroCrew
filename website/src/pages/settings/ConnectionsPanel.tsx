// Settings → OAuth Apps: the operator's OAuth clients for providers whose
// remote MCP server refuses dynamic client registration (GitHub, Asana, ...).
//
// One card per pre-registered registry provider. Each card is the single place
// a registration runbook points to ("enter the credentials under Settings →
// Connections → <Provider>"), so it carries everything that runbook's last step
// needs: the exact redirect URI to copy into the vendor console, the Client ID
// field, the Client secret field, where the stored halves came from (dashboard
// vs environment) and a link back to the runbook itself.
//
// Custody, for the reviewer: the client id is public and lives in config.json;
// the secret goes to the encrypted vault through PUT /api/connections/oauth-clients
// and is only ever reported back as a boolean. Neither half is a user's OAuth
// grant -- that stays with kiro-cli, unchanged.
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BookOpen, Check, Copy, Lock, Trash2 } from 'lucide-react'
import { SettingsCard, SettingsInput, SettingsSection } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { api, type ConnectionOAuthClient, type ConnectionOAuthClientSave } from '../../api/client'
import { copyToClipboard } from '../../utils/clipboard'
import { i18nT } from '../../i18n/t'
import { CONNECTION_PROVIDERS } from '../connections/registry'

export const OAUTH_CLIENTS_QUERY_KEY = ['connections', 'oauth-clients'] as const

/** Repository docs are the runbook's home; the link resolves against main. */
const RUNBOOK_BASE = 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/'

/** `[data-setting-id]` the gallery card deep-links to for one provider. */
export function oauthClientSettingId(slug: string): string {
  return `connections-oauth-client-${slug}`
}

function envName(slug: string, half: 'CLIENT_ID' | 'CLIENT_SECRET'): string {
  return `KIROCREW_CONNECTIONS_${slug.toUpperCase().replace(/-/g, '_')}_${half}`
}

function CopyRedirectUri({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), 1500)
    return () => window.clearTimeout(timer)
  }, [copied])
  const copy = async () => {
    // `copyToClipboard` answers false when both the async API and the
    // execCommand fallback refuse (a non-secure context, a denied permission);
    // a rejection is the same outcome. Neither may read as "Copied".
    let ok = false
    try {
      ok = await copyToClipboard(value)
    } catch {
      ok = false
    }
    setCopyFailed(!ok)
    setCopied(ok)
  }
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 truncate rounded border border-border bg-bg px-2 py-1 text-[12px] text-text" title={value}>
          {value}
        </code>
        <Btn
          onClick={() => { void copy() }}
          aria-label={i18nT('pages.settings.connectionsPanel.copy_redirect_uri')}
        >
          {copied ? <Check className="w-3.5 h-3.5" aria-hidden="true" /> : <Copy className="w-3.5 h-3.5" aria-hidden="true" />}
          {copied ? i18nT('pages.settings.connectionsPanel.copied') : i18nT('pages.settings.connectionsPanel.copy')}
        </Btn>
      </div>
      {/* No hand-off: this sits inside the card whose Client ID and Client
          secret drafts may be typed but unsaved; the hand-off would unmount
          them. The URI is right there to select by hand. */}
      {copyFailed && (
        <ErrorNotice
          variant="inline"
          message={i18nT('pages.settings.connectionsPanel.copy_failed')}
          onDismiss={() => setCopyFailed(false)}
        />
      )}
    </div>
  )
}

function ClientCard({ client, index, readOnly }: { client: ConnectionOAuthClient; index: number; readOnly: boolean }) {
  const qc = useQueryClient()
  const provider = CONNECTION_PROVIDERS.find(p => p.slug === client.slug)
  const name = provider?.name ?? client.slug
  // Draft state mirrors the channel panels: the id is edited in place, the
  // secret is write-only with a deferred clear, and a save remounts the secret
  // field (formKey) so a stored value never lingers in the draft.
  const [clientId, setClientId] = useState(client.client_id ?? '')
  const [secret, setSecret] = useState('')
  const [secretClear, setSecretClear] = useState(false)
  const [formKey, setFormKey] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [removeConfirm, setRemoveConfirm] = useState(false)
  const removeCancelRef = useRef<HTMLButtonElement>(null)
  // Focus lands on Cancel when the delete arms, so a stray second press backs out.
  useEffect(() => { if (removeConfirm) removeCancelRef.current?.focus() }, [removeConfirm])
  useEffect(() => { setClientId(client.client_id ?? '') }, [client.client_id])

  const envId = client.client_id_source === 'env'
  const envSecret = client.client_secret_source === 'env'

  const saveMut = useMutation({
    mutationFn: (body: ConnectionOAuthClientSave) => api.connectionsOAuthClientSave(client.slug, body),
    onSuccess: () => {
      setSecret('')
      setSecretClear(false)
      setFormKey(k => k + 1)
      setError(null)
      void qc.invalidateQueries({ queryKey: OAUTH_CLIENTS_QUERY_KEY })
      // The gallery's status rows carry `needsClientConfig`; a save flips it.
      void qc.invalidateQueries({ queryKey: ['connections-status'] })
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  })
  const deleteMut = useMutation({
    mutationFn: () => api.connectionsOAuthClientDelete(client.slug),
    onSuccess: () => {
      setClientId('')
      setSecret('')
      setSecretClear(false)
      setFormKey(k => k + 1)
      setError(null)
      setRemoveConfirm(false)
      void qc.invalidateQueries({ queryKey: OAUTH_CLIENTS_QUERY_KEY })
      void qc.invalidateQueries({ queryKey: ['connections-status'] })
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  })

  const trimmedId = clientId.trim()
  const idChanged = trimmedId !== (client.client_id ?? '') && !envId
  const dirty = idChanged || secret.length > 0 || secretClear
  const handleSave = () => {
    const body: ConnectionOAuthClientSave = {}
    if (idChanged) body.client_id = trimmedId
    if (secret) body.client_secret = secret
    else if (secretClear) body.client_secret_clear = true
    if (Object.keys(body).length === 0) return
    saveMut.mutate(body)
  }

  const secretDescription = client.confidential
    ? i18nT('pages.settings.connectionsPanel.client_secret_required', { provider: name })
    : i18nT('pages.settings.connectionsPanel.client_secret_optional', { provider: name })

  return (
    <SettingsCard index={index}>
      {/* `data-setting-label` is the anchor the settings search registry's manual
          entry for this card (settingsManual.ts, labelKey oauth_app + the provider
          name as suffix) resolves to; `data-setting-id` is what the gallery's deep
          link waits for. */}
      <div
        data-setting-id={oauthClientSettingId(client.slug)}
        data-setting-label={i18nT('pages.settings.connectionsPanel.oauth_app')}
        className="space-y-3"
      >
        <div className="flex items-center justify-between gap-3">
          <h4 className="m-0 text-sm font-semibold text-text-strong">{name}</h4>
          <span className={`inline-flex items-center gap-1 text-[11px] font-medium ${client.configured ? 'text-ok' : 'text-muted'}`}>
            {client.configured ? <Check className="w-3.5 h-3.5" aria-hidden="true" /> : <Lock className="w-3.5 h-3.5" aria-hidden="true" />}
            {client.configured
              ? i18nT('pages.settings.connectionsPanel.configured')
              : i18nT('pages.settings.connectionsPanel.not_configured')}
          </span>
        </div>
        <p className="m-0 text-[12.5px] text-muted">
          {i18nT('pages.settings.connectionsPanel.card_intro', { provider: name })}{' '}
          <a
            href={`${RUNBOOK_BASE}${client.registration_guide}`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-accent hover:underline"
          >
            <BookOpen className="w-3.5 h-3.5" aria-hidden="true" />
            {i18nT('pages.settings.connectionsPanel.open_runbook', { provider: name })}
          </a>
        </p>

        <div className="space-y-1">
          <div className="text-[12px] font-medium text-text">{i18nT('pages.settings.connectionsPanel.redirect_uri')}</div>
          <CopyRedirectUri value={client.redirect_uri} />
          <p className="m-0 text-[11.5px] text-muted">{i18nT('pages.settings.connectionsPanel.redirect_uri_help')}</p>
        </div>

        <SettingsInput
          label={i18nT('pages.settings.connectionsPanel.client_id')}
          description={envId
            ? i18nT('pages.settings.connectionsPanel.set_by_environment', { name: envName(client.slug, 'CLIENT_ID') })
            : i18nT('pages.settings.connectionsPanel.client_id_help')}
          value={clientId}
          onChange={setClientId}
          placeholder={i18nT('pages.settings.connectionsPanel.client_id_placeholder')}
          disabled={readOnly || envId}
        />

        {envSecret ? (
          // Not a SecretField: its read-only mode explains a REMOTE session, and
          // this half is read-only for a different reason -- the environment
          // owns it. A disabled masked input with the env-var note says that.
          <SettingsInput
            label={i18nT('pages.settings.connectionsPanel.client_secret')}
            description={i18nT('pages.settings.connectionsPanel.set_by_environment', { name: envName(client.slug, 'CLIENT_SECRET') })}
            value="••••••••"
            onChange={() => {}}
            disabled
          />
        ) : (
          <SecretField
            key={`secret-${formKey}`}
            label={i18nT('pages.settings.connectionsPanel.client_secret')}
            description={secretDescription}
            placeholder={i18nT('pages.settings.connectionsPanel.client_secret_placeholder')}
            isSet={client.client_secret_set}
            preview="••••••••"
            readOnly={readOnly}
            value={secret}
            onChange={setSecret}
            cleared={secretClear}
            onClearedChange={setSecretClear}
          />
        )}

        {/* No hand-off: the hand-off navigates to chat and unmounts this card,
            destroying the client id and secret the operator has typed but not
            yet saved (a save failure is exactly when those drafts still exist). */}
        {error && <ErrorNotice title={i18nT('pages.settings.connectionsPanel.save_failed')} message={error} />}

        {!readOnly && (
          <div className="flex items-center justify-between gap-2">
            {removeConfirm ? (
              // Same arming pattern as the Secrets panel: the delete is armed by
              // the first press and fired by a danger-styled second one, with the
              // consequence spelled out in between -- the secret is unrecoverable
              // in-app and a working connection stops until re-entered.
              <div className="flex flex-wrap items-center gap-2" role="group" aria-label={i18nT('pages.settings.connectionsPanel.remove_client', { provider: name })}>
                <span className="text-[13px] text-warn">
                  {i18nT('pages.settings.connectionsPanel.remove_confirm', { provider: name })}
                </span>
                <Btn danger onClick={() => deleteMut.mutate()} disabled={deleteMut.isPending}>
                  {i18nT('pages.settings.connectionsPanel.remove_confirm_action')}
                </Btn>
                <Btn ref={removeCancelRef} onClick={() => setRemoveConfirm(false)} disabled={deleteMut.isPending}>
                  {i18nT('pages.settings.connectionsPanel.cancel')}
                </Btn>
              </div>
            ) : (
              <Btn
                onClick={() => setRemoveConfirm(true)}
                disabled={deleteMut.isPending || (!client.client_id && !client.client_secret_set) || (envId && envSecret)}
                aria-label={i18nT('pages.settings.connectionsPanel.remove_client', { provider: name })}
              >
                <Trash2 className="w-3.5 h-3.5" aria-hidden="true" />
                {i18nT('pages.settings.connectionsPanel.remove')}
              </Btn>
            )}
            {/* Hidden while the removal is armed: one destructive question, two
                answers -- a third action in the same row is the one a hurried
                reader presses by mistake. */}
            {!removeConfirm && (
              <Btn primary onClick={handleSave} disabled={!dirty || saveMut.isPending}>
                {saveMut.isPending ? i18nT('pages.settings.connectionsPanel.saving') : i18nT('pages.settings.connectionsPanel.save')}
              </Btn>
            )}
          </div>
        )}
      </div>
    </SettingsCard>
  )
}

export function ConnectionsPanel({ readOnly = false }: { readOnly?: boolean }) {
  const { data, isLoading, error } = useQuery({
    queryKey: OAUTH_CLIENTS_QUERY_KEY,
    queryFn: api.connectionsOAuthClients,
  })
  const clients = data?.clients ?? []

  return (
    <div className="space-y-6">
      <SettingsSection title={i18nT('pages.settings.connectionsPanel.title')}>
        <p className="m-0 text-[12.5px] text-muted">{i18nT('pages.settings.connectionsPanel.intro')}</p>
        {error && (
          // A list-load failure: nothing is typed yet, so the hand-off can destroy
          // nothing and the agent is the right next step.
          <ErrorNotice
            title={i18nT('pages.settings.connectionsPanel.load_failed')}
            message={error instanceof Error ? error.message : String(error)}
            askAgent
          />
        )}
        {isLoading && <p className="m-0 text-[12.5px] text-muted">{i18nT('pages.settings.connectionsPanel.loading')}</p>}
        {!isLoading && !error && clients.length === 0 && (
          <p className="m-0 text-[12.5px] text-muted">{i18nT('pages.settings.connectionsPanel.none')}</p>
        )}
        {clients.map((client, index) => (
          <ClientCard key={client.slug} client={client} index={index + 1} readOnly={readOnly} />
        ))}
      </SettingsSection>
    </div>
  )
}
