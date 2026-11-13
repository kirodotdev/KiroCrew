// The OAuth app part of a Connections card: the operator's OAuth client for a
// provider whose remote MCP server refuses dynamic client registration (GitHub,
// Asana, ...). It opens in place on that provider's card in Settings →
// Connections; the Google entry has one for all six Google products.
//
// It is the single place a registration runbook points to ("enter the
// credentials under Settings → Connections → <Provider>"), so it carries everything that runbook's last step
// needs: the exact redirect URI to copy into the vendor console, the Client ID
// field, the Client secret field, where the stored halves came from (dashboard
// vs environment) and a link back to the runbook itself.
//
// Custody, for the reviewer: the client id is public and lives in config.json;
// the secret goes to the encrypted vault through PUT /api/connections/oauth-clients
// and is only ever reported back as a boolean. Neither half is a user's OAuth
// grant -- that stays with kiro-cli, unchanged.
import { createContext, useContext, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BookOpen, Check, Copy, Lock, Terminal, Trash2 } from 'lucide-react'
import { SettingsInput, SettingsToggle } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import AgentSpecNotRewrittenNotice, { agentSpecNotRewritten } from '../../components/AgentSpecNotRewrittenNotice'
import { api, ApiError, type ConnectionOAuthClient, type ConnectionOAuthClientSave } from '../../api/client'
import { copyToClipboard } from '../../utils/clipboard'
import { i18nT } from '../../i18n/t'
import { fmtList } from '../../i18n/format'
import { CONNECTION_PROVIDERS } from './registry'
import { isGoogleSlug } from './connectionsLinks'

export const OAUTH_CLIENTS_QUERY_KEY = ['connections', 'oauth-clients'] as const

/** Repository docs are the runbook's home; the link resolves against main. */
const RUNBOOK_BASE = 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/'

/** The Google entry's setup scripts and private-app guide, on main. */
const GOOGLE_SETUP_SCRIPT = 'https://github.com/kirodotdev/KiroCrew/blob/main/scripts/google-oauth-setup'
const GOOGLE_PRIVATE_APP_GUIDE = `${RUNBOOK_BASE}oauth-app-registration/google.md`

/** `[data-setting-id]` the settings search entry for one card waits for. */
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

/** A shared Google save that this card took but some other Google cards did not. */
class PartialShareError extends Error {
  constructor(readonly failed: string[]) {
    super(failed.join(', '))
  }
}

interface RedirectRow { slug: string; name: string; uri: string }

/** Set by the Connections page; outside it a draft has no hand-off to guard. */
export const OAuthDraftsContext = createContext<(settingSlug: string, dirty: boolean) => void>(() => {})
/** Set by the Connections page. A client save or delete the backend accepts
 *  cancels that slug's in-flight sign-in (`cancel_mint`); its poll then reads
 *  `idle`, which never settles a wait, so the page drops those waits itself. */
export const OAuthClientWrittenContext = createContext<(slugs: string[]) => void>(() => {})

/** One form's unsaved input, as `ClientCard` holds it. */
export interface OAuthDraftValues { clientId: string; secret: string; secretClear: boolean }
/** Set by the Connections page, which outlives the forms it swaps; outside it
 *  nothing is kept, so a draft never outlives the page that holds it. */
export interface OAuthDraftValueStore {
  get: (key: string) => OAuthDraftValues | undefined
  set: (key: string, values: OAuthDraftValues) => void
}
export const OAuthDraftValuesContext = createContext<OAuthDraftValueStore>({ get: () => undefined, set: () => {} })

/** The backend committed the write (and cancelled the sign-in) but could not
 *  rebuild the agent spec, so it still answers 500. */
function writeCommitted(e: unknown): boolean {
  if (!(e instanceof ApiError)) return false
  try {
    return (JSON.parse(e.body) as { code?: unknown }).code === 'projection_refresh_failed'
  } catch {
    return false
  }
}

function ClientCard({ client, readOnly, shareWith, settingSlug, redirects }: {
  client: ConnectionOAuthClient
  readOnly: boolean
  shareWith: ConnectionOAuthClient[]
  /** The settings search id this part answers to (`google` for the Google entry). */
  settingSlug: string
  /** Every product's redirect URI for the Google entry; one row otherwise. */
  redirects: RedirectRow[]
}) {
  const qc = useQueryClient()
  const clientWritten = useContext(OAuthClientWrittenContext)
  // Runs one PUT or DELETE and tells the page when the backend committed it,
  // including a commit whose agent-spec rebuild then failed.
  const written = async <T,>(slug: string, call: () => Promise<T>): Promise<T> => {
    try {
      const r = await call()
      clientWritten([slug])
      return r
    } catch (e) {
      if (writeCommitted(e)) clientWritten([slug])
      throw e
    }
  }
  const provider = CONNECTION_PROVIDERS.find(p => p.slug === client.slug)
  const google = redirects.length > 1
  // The Google entry's one part speaks for every Google product, not the first.
  const name = google ? i18nT('pages.connectionsPage.google_entry_name') : provider?.name ?? client.slug
  // Draft state mirrors the channel panels: the id is edited in place, the
  // secret is write-only with a deferred clear, and a save remounts the secret
  // field (formKey) so a stored value never lingers in the draft.
  // A page that swaps one form for another (the config read landing, or the
  // gallery flag changing) remounts this card; the page's draft store hands
  // the new one what the old one held, so a typed id or secret survives.
  const drafts = useContext(OAuthDraftValuesContext)
  const draftKey = `${settingSlug}:${client.slug}`
  const kept = drafts.get(draftKey)
  const [clientId, setClientId] = useState(kept?.clientId ?? client.client_id ?? '')
  const [secret, setSecret] = useState(kept?.secret ?? '')
  const [secretClear, setSecretClear] = useState(kept?.secretClear ?? false)
  useEffect(() => {
    drafts.set(draftKey, { clientId, secret, secretClear })
  }, [drafts, draftKey, clientId, secret, secretClear])
  const [formKey, setFormKey] = useState(0)
  const [error, setError] = useState<string | null>(null)
  // The change was saved but the agent spec did not follow it.
  const [notApplied, setNotApplied] = useState(false)
  const [removeConfirm, setRemoveConfirm] = useState(false)
  // On by default: one Google app serves every Google card, so the person
  // pastes the client once instead of six times.
  const [shareAll, setShareAll] = useState(true)
  // Product names a shared save did not reach, and whether Save stays armed to retry them.
  const [partialFailed, setPartialFailed] = useState<string[] | null>(null)
  const [retryShared, setRetryShared] = useState(false)
  const removeCancelRef = useRef<HTMLButtonElement>(null)
  // Focus lands on Cancel when the delete arms, so a stray second press backs out.
  useEffect(() => { if (removeConfirm) removeCancelRef.current?.focus() }, [removeConfirm])
  // Follow the stored id only while nothing on this card is unsaved: another
  // card's shared save refetches this one, and swapping the id under an
  // unsaved id or secret would make the next Save send a mismatched pair.
  const storedIdRef = useRef(client.client_id ?? '')
  const secretPendingRef = useRef(false)
  secretPendingRef.current = secret.length > 0 || secretClear
  useEffect(() => {
    const next = client.client_id ?? ''
    // Read both refs now: React may run the updater after the ref moves on.
    const prev = storedIdRef.current
    const pending = secretPendingRef.current
    setClientId(draft => (draft === prev && !pending ? next : draft))
    storedIdRef.current = next
  }, [client.client_id])

  const envId = client.client_id_source === 'env'
  const envSecret = client.client_secret_source === 'env'

  const saveMut = useMutation({
    mutationFn: async (body: ConnectionOAuthClientSave) => {
      const own = await written(client.slug, () => api.connectionsOAuthClientSave(client.slug, body))
      // Only a whole pair travels: a non-empty Client ID plus a new secret.
      // Anything less (an id alone, a secret with no id, a clear) would leave
      // another card's half next to this card's, so it stays on this card.
      const id = clientId.trim()
      if (!shareAll || shareWith.length === 0 || !id || !body.client_secret) return own
      // A card whose id or secret is set by the environment is skipped: a
      // stored half there would pair with the environment's other half.
      // Sequential: each PUT rewrites the agent spec.
      const shared: ConnectionOAuthClientSave = { client_id: id, client_secret: body.client_secret }
      let result = own
      const failed: string[] = []
      for (const peer of shareWith) {
        if (peer.client_id_source === 'env' || peer.client_secret_source === 'env') continue
        try {
          const r = await written(peer.slug, () => api.connectionsOAuthClientSave(peer.slug, shared))
          if (agentSpecNotRewritten(r)) result = r
        } catch {
          failed.push(CONNECTION_PROVIDERS.find(p => p.slug === peer.slug)?.name ?? peer.slug)
        }
      }
      if (failed.length > 0) {
        // This card did save: refresh the badges, and keep Save armed for a
        // retry even though the refetched id no longer differs from the draft.
        setRetryShared(true)
        void qc.invalidateQueries({ queryKey: OAUTH_CLIENTS_QUERY_KEY })
        throw new PartialShareError(failed)
      }
      return result
    },
    onSuccess: (r) => {
      setNotApplied(agentSpecNotRewritten(r))
      setSecret('')
      setSecretClear(false)
      setFormKey(k => k + 1)
      setError(null)
      setPartialFailed(null)
      setRetryShared(false)
      void qc.invalidateQueries({ queryKey: OAUTH_CLIENTS_QUERY_KEY })
      // The gallery's status rows carry `needsClientConfig`; a save flips it.
      void qc.invalidateQueries({ queryKey: ['connections-status'] })
    },
    onError: (e: unknown) => {
      setPartialFailed(e instanceof PartialShareError ? e.failed : null)
      setError(e instanceof Error ? e.message : String(e))
    },
  })
  // The Google part removes the stored client of every Google product its Save
  // writes, which skips any product with a half set by the environment (see the
  // loop below). Its own slug always gets the DELETE: DELETE clears only what
  // is stored, so an override on it stays.
  const ownStored = !!(client.client_id || client.client_secret_set) && !(envId && envSecret)
  const sharedGoogle = shareWith.length > 0
  // Only peers the delete loop below would reach count: one with a half set by
  // the environment is skipped there, so it cannot make Remove look possible.
  const anyClient = ownStored || shareWith.some(peer =>
    peer.client_id_source !== 'env' && peer.client_secret_source !== 'env' && (peer.client_id || peer.client_secret_set))
  const deleteMut = useMutation({
    mutationFn: async () => {
      let result = ownStored || sharedGoogle
        ? await written(client.slug, () => api.connectionsOAuthClientDelete(client.slug))
        : undefined
      let firstError: unknown
      for (const peer of shareWith) {
        // A product with any half set by the environment is skipped, the same
        // as the shared Save: a partly owned one has its own form and Remove
        // below this one, and deleting here would wipe the stored half that
        // pairs with the environment's.
        if (peer.client_id_source === 'env' || peer.client_secret_source === 'env') continue
        try {
          const r = await written(peer.slug, () => api.connectionsOAuthClientDelete(peer.slug))
          if (agentSpecNotRewritten(r)) result = r
        } catch (e) {
          firstError ??= e
        }
      }
      if (firstError !== undefined) {
        void qc.invalidateQueries({ queryKey: OAUTH_CLIENTS_QUERY_KEY })
        throw firstError
      }
      return result
    },
    onSuccess: (r) => {
      setNotApplied(agentSpecNotRewritten(r))
      // Remove clears only stored halves: an environment-set Client ID stays.
      setClientId(envId ? (client.client_id ?? '') : '')
      setSecret('')
      setSecretClear(false)
      setFormKey(k => k + 1)
      setError(null)
      setPartialFailed(null)
      setRetryShared(false)
      setRemoveConfirm(false)
      void qc.invalidateQueries({ queryKey: OAUTH_CLIENTS_QUERY_KEY })
      void qc.invalidateQueries({ queryKey: ['connections-status'] })
    },
    onError: (e: unknown) => {
      setPartialFailed(null)
      setError(e instanceof Error ? e.message : String(e))
    },
  })

  const trimmedId = clientId.trim()
  const idChanged = trimmedId !== (client.client_id ?? '') && !envId
  const dirty = idChanged || secret.length > 0 || secretClear || retryShared
  // Tell the page: while any form holds unsaved input, no card offers the
  // chat hand-off, which navigates away and would drop it.
  const reportDraft = useContext(OAuthDraftsContext)
  useEffect(() => {
    reportDraft(settingSlug, dirty)
    return () => reportDraft(settingSlug, false)
  }, [reportDraft, settingSlug, dirty])
  const handleSave = () => {
    const body: ConnectionOAuthClientSave = {}
    // A new secret always travels with the id it was typed next to, so no
    // write (this card's or another card's fan-out) can pair it with a
    // different stored id.
    if (idChanged || ((retryShared || secret) && trimmedId && !envId)) body.client_id = trimmedId
    if (secret) body.client_secret = secret
    else if (secretClear) body.client_secret_clear = true
    if (Object.keys(body).length === 0) return
    saveMut.mutate(body)
  }

  // Labels held in variables on purpose: the settings index skips them, so a
  // generic "Client ID" entry cannot highlight a field inside a closed card.
  // settingsManual.ts indexes each provider's OAuth app, and that highlight
  // opens the card first.
  const clientIdLabel = i18nT('pages.settings.connectionsPanel.client_id')
  const clientSecretLabel = i18nT('pages.settings.connectionsPanel.client_secret')
  const secretDescription = client.confidential
    ? i18nT('pages.settings.connectionsPanel.client_secret_required', { provider: name })
    : i18nT('pages.settings.connectionsPanel.client_secret_optional', { provider: name })

  return (
    <>
      {/* `data-setting-label` is the anchor the settings search registry's manual
          entry for this part (settingsManual.ts, labelKey oauth_app + the provider
          name as suffix) resolves to; `data-setting-id` is what a search hit or an
          old OAuth Apps link waits for once the card has opened this part. */}
      <div
        data-setting-id={oauthClientSettingId(settingSlug)}
        data-setting-label={i18nT('pages.settings.connectionsPanel.oauth_app')}
        className="space-y-3"
      >
        <div className="flex items-center justify-between gap-3">
          <h4 className="m-0 text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.connectionsPanel.oauth_app')}</h4>
          <span className={`inline-flex items-center gap-1 text-[11px] font-medium ${client.configured ? 'text-ok' : 'text-muted'}`}>
            {client.configured ? <Check className="w-3.5 h-3.5" aria-hidden="true" /> : <Lock className="w-3.5 h-3.5" aria-hidden="true" />}
            {client.configured
              ? i18nT('pages.settings.connectionsPanel.configured')
              : i18nT('pages.settings.connectionsPanel.not_configured')}
          </span>
        </div>
        <p className="m-0 text-[12.5px] text-muted">
          {i18nT('pages.settings.connectionsPanel.card_intro', { provider: name })}{' '}
          {google ? (
            <>
              <a href={GOOGLE_PRIVATE_APP_GUIDE} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">
                <BookOpen className="w-3.5 h-3.5" aria-hidden="true" />
                {i18nT('pages.connectionsPage.google_private_app_guide')}
              </a>{' '}
              {/* The PowerShell twin on Windows, the shell script elsewhere. */}
              <a
                href={`${GOOGLE_SETUP_SCRIPT}${typeof navigator !== 'undefined' && /Win/.test(navigator.platform) ? '.ps1' : '.sh'}`}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-accent hover:underline"
              >
                <Terminal className="w-3.5 h-3.5" aria-hidden="true" />
                {i18nT('pages.connectionsPage.google_setup_script')}
              </a>
            </>
          ) : (
            <a
              href={`${RUNBOOK_BASE}${client.registration_guide}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-accent hover:underline"
            >
              <BookOpen className="w-3.5 h-3.5" aria-hidden="true" />
              {i18nT('pages.settings.connectionsPanel.open_runbook', { provider: name })}
            </a>
          )}
        </p>

        <div className="space-y-1">
          <div className="text-[12px] font-medium text-text">
            {google ? i18nT('pages.connectionsPage.google_redirect_uris') : i18nT('pages.settings.connectionsPanel.redirect_uri')}
          </div>
          {redirects.map(row => (
            <div key={row.slug} className="min-w-0">
              {google && <div className="text-[11.5px] text-muted">{row.name}</div>}
              <CopyRedirectUri value={row.uri} />
            </div>
          ))}
          <p className="m-0 text-[11.5px] text-muted">{i18nT('pages.settings.connectionsPanel.redirect_uri_help')}</p>
        </div>

        <SettingsInput
          label={clientIdLabel}
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
            label={clientSecretLabel}
            description={i18nT('pages.settings.connectionsPanel.set_by_environment', { name: envName(client.slug, 'CLIENT_SECRET') })}
            value="••••••••"
            onChange={() => {}}
            disabled
          />
        ) : (
          <SecretField
            key={`secret-${formKey}`}
            label={clientSecretLabel}
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

        {!readOnly && shareWith.length > 0 && (
          <SettingsToggle
            label={i18nT('pages.settings.connectionsPanel.share_google', { products: fmtList(shareWith.map(p => CONNECTION_PROVIDERS.find(c => c.slug === p.slug)?.name ?? p.slug), { style: 'narrow' }) })}
            description={i18nT('pages.settings.connectionsPanel.share_google_help')}
            checked={shareAll}
            onChange={setShareAll}
          />
        )}

        {/* No hand-off: the hand-off navigates to chat and unmounts this card,
            destroying the client id and secret the operator has typed but not
            yet saved (a save failure is exactly when those drafts still exist). */}
        {error && (
          <ErrorNotice
            title={partialFailed
              ? i18nT('pages.settings.connectionsPanel.share_google_partial_title', { products: fmtList(partialFailed, { style: 'narrow' }) })
              : i18nT('pages.settings.connectionsPanel.save_failed')}
            message={partialFailed
              // The product the save landed on, not the entry's "Google".
              ? i18nT('pages.settings.connectionsPanel.share_google_partial', { provider: provider?.name ?? client.slug })
              : error}
          />
        )}
        {/* Same reason, no hand-off: it would unmount the card's unsaved drafts. */}
        {notApplied && <AgentSpecNotRewrittenNotice askAgent={false} />}

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
                {/* Not while a shared Save is still writing: its later PUTs would
                    recreate the clients this Remove just deleted. */}
                <Btn danger onClick={() => deleteMut.mutate()} disabled={deleteMut.isPending || saveMut.isPending}>
                  {i18nT('pages.settings.connectionsPanel.remove_confirm_action')}
                </Btn>
                <Btn ref={removeCancelRef} onClick={() => setRemoveConfirm(false)} disabled={deleteMut.isPending}>
                  {i18nT('pages.settings.connectionsPanel.cancel')}
                </Btn>
              </div>
            ) : (
              <Btn
                onClick={() => setRemoveConfirm(true)}
                disabled={deleteMut.isPending || saveMut.isPending || !anyClient}
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
    </>
  )
}

/**
 * The OAuth app part for `slugs`: one provider, or the six Google products,
 * which share one form (a save fills every product unless the person opts out)
 * and list every product's redirect URI.
 */
export function OAuthAppPanel({ slugs, settingSlug, readOnly = false }: { slugs: string[]; settingSlug: string; readOnly?: boolean }) {
  const { data, isLoading, error } = useQuery({
    queryKey: OAUTH_CLIENTS_QUERY_KEY,
    queryFn: api.connectionsOAuthClients,
  })
  if (error && !data) {
    // A first-load failure: nothing is typed yet, so the hand-off can destroy
    // nothing and the agent is the right next step.
    return (
      <ErrorNotice
        title={i18nT('pages.settings.connectionsPanel.load_failed')}
        message={error instanceof Error ? error.message : String(error)}
        askAgent
      />
    )
  }
  if (isLoading) return <p className="m-0 text-[12.5px] text-muted">{i18nT('pages.settings.connectionsPanel.loading')}</p>
  // In `slugs` order: the first listed slug is the one this part saves.
  const clients = slugs.flatMap(slug => (data?.clients ?? []).filter(c => c.slug === slug))
  // The form edits the first product the environment does not own, so an
  // environment-set Gmail does not lock the shared Google form.
  const primary = clients.find(c => c.client_id_source !== 'env' && c.client_secret_source !== 'env') ?? clients[0]
  if (!primary) return <p className="m-0 text-[12.5px] text-muted">{i18nT('pages.settings.connectionsPanel.none')}</p>
  // A failed REFETCH keeps the cached list and the form, so drafts survive;
  // the notice has no hand-off, which would navigate away from them.
  const refetchError = error ? (
    <ErrorNotice
      title={i18nT('pages.settings.connectionsPanel.load_failed')}
      message={error instanceof Error ? error.message : String(error)}
      className="mb-3"
    />
  ) : null
  // A Google product whose stored client differs from the one this part
  // shows (saved on its own before the products shared one form) gets its own
  // card below, like a partly environment-owned one, and stays out of this
  // part's shared Save and Remove: they would overwrite or delete a client the
  // person never saw here.
  const ownForm = (c: ConnectionOAuthClient) => c !== primary && (
    (c.client_id_source === 'env') !== (c.client_secret_source === 'env')
    || (c.client_id_source !== 'env' && !!c.client_id && c.client_id !== (primary.client_id ?? ''))
  )
  return (
    <>
    {refetchError}
    <ClientCard
      key={primary.slug}
      client={primary}
      readOnly={readOnly}
      shareWith={isGoogleSlug(primary.slug) ? clients.filter(c => c.slug !== primary.slug && !ownForm(c)) : []}
      settingSlug={settingSlug}
      redirects={clients.map(c => ({
        slug: c.slug,
        name: CONNECTION_PROVIDERS.find(p => p.slug === c.slug)?.name ?? c.slug,
        uri: c.redirect_uri,
      }))}
    />
    {/* A Google product with one half set by the environment cannot take the
        shared save (it would pair a stored half with the environment's), and
        one holding a different stored client must not be overwritten by it.
        Each keeps its own form here; an environment's half is shown read-only
        and never written. */}
    {clients
      .filter(ownForm)
      .map(c => (
        <div key={c.slug} className="mt-4 border-t border-border pt-4">
          <ClientCard
            client={c}
            readOnly={readOnly}
            shareWith={[]}
            settingSlug={c.slug}
            redirects={[{ slug: c.slug, name: CONNECTION_PROVIDERS.find(p => p.slug === c.slug)?.name ?? c.slug, uri: c.redirect_uri }]}
          />
        </div>
      ))}
    </>
  )
}
