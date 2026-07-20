// Share panel for publishing a KiroCrew artifact to a sharing provider
// (Mesh-1880). Rendered in the artifact detail page and (via the same
// component) the file side-panel after an auto-save. Handles both the
// unpublished state (visibility + alias picker → Publish) and the published
// state (stable link + copy, sharing management, version-sync status,
// conflict banner, un-share / unpublish).

import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  Check,
  Copy,
  ExternalLink,
  Globe,
  Lock,
  Radio,
  RefreshCw,
  Trash2,
  Users,
  X,
} from 'lucide-react'
import { api } from '../api/client'
import { Card, Btn, Badge, Input } from './ui'
import type { Artifact, ArtifactPublication, PublishProviderDescriptor } from '../types'

type Visibility = 'PRIVATE' | 'SHARED' | 'PUBLIC'

const VIS_OPTS: { value: Visibility; label: string; icon: typeof Lock; hint: string }[] = [
  { value: 'PRIVATE', label: 'Private', icon: Lock, hint: 'Only you' },
  { value: 'SHARED', label: 'Shared', icon: Users, hint: 'Specific people' },
  { value: 'PUBLIC', label: 'Public', icon: Globe, hint: 'Everyone in your organization' },
]

function errMsg(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e)
  // Backend errors arrive as a JSON body (optionally prefixed with a status
  // code, e.g. `400: {"error":"…"}`). Surface the human-readable `error`
  // field rather than the raw JSON blob.
  const m = raw.match(/"error"\s*:\s*"([^"]+)"/)
  return m ? m[1] : raw
}

/**
 * A publish provider's `view_url` is provider-controlled (in a companion edition
 * it comes back from an external service), so it must not be trusted as an
 * `href` verbatim — a `javascript:`/`data:` scheme would be a stored-XSS vector
 * when clicked. Return the URL only when it parses as http(s); otherwise `undefined`
 * so callers render a disabled affordance instead of a dangerous link.
 */
function safeHttpUrl(url: string | null | undefined): string | undefined {
  if (!url) return undefined
  try {
    const u = new URL(url, window.location.origin)
    return u.protocol === 'https:' || u.protocol === 'http:' ? u.href : undefined
  } catch {
    return undefined
  }
}

export function ArtifactSharePanel({
  artifact,
  onClose,
}: {
  artifact: Artifact
  onClose?: () => void
}) {
  const slug = artifact.slug
  const pub: ArtifactPublication | null = artifact.publication ?? null
  const qc = useQueryClient()

  const [visibility, setVisibility] = useState<Visibility>(pub?.visibility ?? 'PRIVATE')
  const [aliases, setAliases] = useState<string[]>(pub?.shared_with ?? [])
  const [aliasInput, setAliasInput] = useState('')
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Selected publishing destination (unpublished form). A published artifact is
  // bound to pub.provider and this is ignored.
  const [provider, setProvider] = useState<string>(pub?.provider ?? 'artifactory')

  // Providers that can host THIS artifact's kind, with their sharing/sync
  // descriptors. Drives the picker (shown only when >1 capable provider) and
  // the per-provider sharing controls (Mesh-2445).
  const { data: providersData } = useQuery({
    queryKey: ['publish-providers', artifact.kind],
    queryFn: () => api.getArtifactPublishProviders(artifact.kind),
  })
  const providers: PublishProviderDescriptor[] = providersData?.providers ?? []
  const capableProviders = providers.filter((p) => p.capable)

  // Auto-select a sensible default once the list loads: Artifactory if it can
  // host this kind, else the first capable provider. Unpublished form only.
  useEffect(() => {
    if (pub || capableProviders.length === 0) return
    if (!capableProviders.some((p) => p.name === provider)) {
      const preferred =
        capableProviders.find((p) => p.name === 'artifactory') ?? capableProviders[0]
      setProvider(preferred.name)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providersData, pub])

  const selDesc = providers.find((p) => p.name === provider)
  const providerLabel = (n?: string) =>
    providers.find((p) => p.name === n)?.display_name ||
    (n === 'chorus' ? 'Chorus' : 'Artifactory')

  // Only offer visibility options the provider supports (e.g. MarkBin has no Shared).
  const allowsPublic = selDesc ? selDesc.sharing_model.supports_public : true
  const allowsShared = selDesc ? selDesc.sharing_model.supports_shared : true
  const visOpts = VIS_OPTS.filter(
    (o) => (o.value !== 'PUBLIC' || allowsPublic) && (o.value !== 'SHARED' || allowsShared),
  )

  // Track the "copied" reset timer so we can clear it on unmount. Without
  // this, a timeout scheduled by copyLink() can fire after the component
  // (and, in tests, the whole jsdom environment) has been torn down, calling
  // setCopied on an unmounted tree → "ReferenceError: window is not defined".
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (copyTimer.current) clearTimeout(copyTimer.current)
    },
    [],
  )

  const invalidate = () => qc.invalidateQueries({ queryKey: ['artifact', slug] })

  const publishMut = useMutation({
    mutationFn: (body: { visibility: Visibility; shared_with: string[]; provider?: string }) =>
      api.publishArtifact(slug, body),
    onSuccess: () => {
      setError(null)
      invalidate()
    },
    onError: (e) => setError(errMsg(e)),
  })
  const sharingMut = useMutation({
    mutationFn: (body: { visibility: Visibility; shared_with: string[] }) =>
      api.updateArtifactSharing(slug, body),
    onSuccess: () => {
      setError(null)
      invalidate()
    },
    onError: (e) => {
      setError(errMsg(e))
      // The optimistic local selection didn't land — snap back to server truth.
      setVisibility(pub?.visibility ?? 'PRIVATE')
      setAliases(pub?.shared_with ?? [])
    },
  })
  const unpublishMut = useMutation({
    mutationFn: () => api.unpublishArtifact(slug),
    onSuccess: () => {
      setError(null)
      invalidate()
    },
    onError: (e) => setError(errMsg(e)),
  })

  const busy = publishMut.isPending || sharingMut.isPending || unpublishMut.isPending

  function addAlias(raw: string) {
    // Accept full addresses (e.g. "alice@example.com") from copy/paste and
    // strip everything from the first "@" — aliases never contain one — so
    // users don't hit validation errors. Also drop a trailing comma/space.
    const a = raw.trim().replace(/[,\s]+$/, '').split('@')[0].trim()
    if (!a) return
    setAliases((prev) => (prev.includes(a) ? prev : [...prev, a]))
    setAliasInput('')
  }
  function removeAlias(a: string) {
    setAliases((prev) => prev.filter((x) => x !== a))
  }

  function copyLink() {
    if (!pub?.view_url) return
    navigator.clipboard?.writeText(pub.view_url).then(
      () => {
        setCopied(true)
        if (copyTimer.current) clearTimeout(copyTimer.current)
        copyTimer.current = setTimeout(() => setCopied(false), 1500)
      },
      () => setError('Could not copy to clipboard'),
    )
  }

  // ── Unpublished: the publish form ──────────────────────────────────────
  if (!pub) {
    // A provider whose sharing is not API-programmable (e.g. Chorus — managed
    // in its own web UI) gets no visibility/alias controls here.
    const programmable = selDesc ? selDesc.sharing_model.programmable : true
    const needsAliases = programmable && visibility === 'SHARED' && aliases.length === 0
    const degraded = selDesc && selDesc.kind_support !== 'native'
    return (
      <Card className="mb-3">
        <div className="flex items-center justify-between mb-2">
          <div className="text-[13px] font-medium text-text">
            Share to {providerLabel(provider)}
          </div>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="p-1 rounded text-muted hover:text-text bg-transparent border-none cursor-pointer"
              aria-label="Close share panel"
            >
              <X size={13} />
            </button>
          )}
        </div>
        {/* Provider picker — only when more than one provider can host this kind. */}
        {capableProviders.length > 1 && (
          <div className="flex flex-wrap gap-1.5 mb-3">
            {capableProviders.map((p) => (
              <button
                key={p.name}
                type="button"
                onClick={() => setProvider(p.name)}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] border transition-colors cursor-pointer ${
                  provider === p.name
                    ? 'border-accent text-accent bg-accent-subtle'
                    : 'border-border text-muted hover:text-text hover:border-border-strong bg-transparent'
                }`}
                title={p.sync_model.collab_mode === 'live' ? 'Live collaborative doc' : ''}
              >
                {p.sync_model.collab_mode === 'live' && <Radio size={12} className="lucide-inline" />}
                {p.display_name}
              </button>
            ))}
          </div>
        )}
        {degraded && (
          <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-warn/40 bg-warn-subtle text-[12px] text-warn">
            <AlertCircle size={13} className="lucide-inline shrink-0 mt-0.5" />
            <span>
              {providerLabel(provider)} stores this as text — a <strong>{artifact.kind}</strong>{' '}
              artifact won&apos;t render there.
            </span>
          </div>
        )}
        {selDesc?.available === false && (
          <div className="mb-3 text-[11.5px] text-muted">
            {providerLabel(provider)} tooling isn&apos;t installed yet — it installs automatically
            on your first publish (may take a moment).
          </div>
        )}
        {programmable ? (
          <>
            <div className="flex flex-wrap gap-1.5 mb-3">
              {visOpts.map(({ value, label, icon: Icon, hint }) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setVisibility(value)}
                  className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] border transition-colors cursor-pointer ${
                    visibility === value
                      ? 'border-accent text-accent bg-accent-subtle'
                      : 'border-border text-muted hover:text-text hover:border-border-strong bg-transparent'
                  }`}
                  title={hint}
                >
                  <Icon size={12} /> {label}
                </button>
              ))}
            </div>
            {visibility === 'SHARED' && (
              <AliasEditor
                aliases={aliases}
                aliasInput={aliasInput}
                setAliasInput={setAliasInput}
                addAlias={addAlias}
                removeAlias={removeAlias}
              />
            )}
            {visibility === 'PUBLIC' && (
              <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-warn/40 bg-warn-subtle text-[12px] text-warn">
                <AlertCircle size={13} className="lucide-inline shrink-0 mt-0.5" />
                <span>Public artifacts are visible to <strong>everyone in your organization</strong>.</span>
              </div>
            )}
          </>
        ) : (
          <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-border bg-bg-elevated text-[12px] text-muted">
            <Radio size={13} className="lucide-inline shrink-0 mt-0.5" />
            <span>
              {providerLabel(provider)} is a live collaborative doc. After publishing, manage
              who can see it in {providerLabel(provider)}.
            </span>
          </div>
        )}
        {error && <ShareError msg={error} />}
        <Btn
          primary
          disabled={busy || needsAliases}
          onClick={() => {
            if (programmable && visibility === 'PUBLIC') {
              if (!window.confirm('Make this visible to everyone in your organization?')) return
            }
            publishMut.mutate({
              visibility: programmable ? visibility : 'PRIVATE',
              shared_with: programmable && visibility === 'SHARED' ? aliases : [],
              provider,
            })
          }}
        >
          {publishMut.isPending ? 'Publishing…' : `Publish to ${providerLabel(provider)}`}
        </Btn>
      </Card>
    )
  }

  // ── Published: link + management ────────────────────────────────────────
  const kiroV = artifact.version
  const artiV = pub.version_map?.[String(pub.last_synced_kirocrew_version)]
  const isLive = pub.collab_mode === 'live'
  const provLabel = providerLabel(pub.provider)
  // The provider-controlled view_url is only trusted as an href when it is
  // http(s) — a javascript:/data: scheme would be a click-XSS vector.
  const safeViewUrl = safeHttpUrl(pub.view_url)
  return (
    <Card className="mb-3">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <div className="text-[13px] font-medium text-text">Published to {provLabel}</div>
          {isLive ? (
            <Badge variant="ok">
              <span className="inline-flex items-center gap-1">
                <Radio size={10} /> live
              </span>
            </Badge>
          ) : (
            <Badge variant="ok">{pub.visibility.toLowerCase()}</Badge>
          )}
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded text-muted hover:text-text bg-transparent border-none cursor-pointer"
            aria-label="Close share panel"
          >
            <X size={13} />
          </button>
        )}
      </div>

      {/* Stable link + copy */}
      <div className="flex items-center gap-2 mb-3">
        <input
          readOnly
          value={pub.view_url}
          onFocus={(e) => e.currentTarget.select()}
          className="flex-1 text-[12px] px-2 py-1.5 rounded-md bg-bg-elevated border border-border text-muted font-mono outline-none"
          aria-label="Share link"
        />
        <button
          type="button"
          onClick={copyLink}
          className="p-1.5 rounded text-muted hover:text-text bg-transparent border border-border cursor-pointer transition-colors"
          title="Copy link"
          aria-label="Copy link"
        >
          {copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
        </button>
        <a
          href={safeViewUrl ?? undefined}
          {...(safeViewUrl ? { target: '_blank', rel: 'noreferrer' } : { 'aria-disabled': true })}
          className={`p-1.5 rounded border border-border transition-colors inline-flex ${
            safeViewUrl
              ? 'text-muted hover:text-text'
              : 'text-muted/40 pointer-events-none cursor-not-allowed'
          }`}
          title={safeViewUrl ? `Open in ${provLabel}` : 'Link unavailable (unsafe URL)'}
          aria-label={safeViewUrl ? `Open in ${provLabel}` : 'Link unavailable (unsafe URL)'}
        >
          <ExternalLink size={13} />
        </a>
      </div>

      {/* Conflict banner */}
      {pub.last_error && (
        <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-danger/40 bg-danger-subtle text-[12px] text-danger">
          <AlertCircle size={13} className="lucide-inline shrink-0 mt-0.5" />
          <div className="flex-1">
            <div><strong>Sync issue:</strong> {pub.last_error}</div>
            {!isLive && (
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  publishMut.mutate({ visibility: pub.visibility, shared_with: pub.shared_with })
                }
                className="mt-1 inline-flex items-center gap-1 text-[12px] text-danger hover:underline bg-transparent border-none cursor-pointer p-0 disabled:opacity-40"
              >
                <RefreshCw size={11} /> Force re-sync
              </button>
            )}
          </div>
        </div>
      )}

      {/* Visibility switcher — selecting Shared reveals the alias editor
          below before committing, so people can be added even when currently
          Private. Selecting Private/Public applies immediately. For a LIVE
          (CRDT) provider there is no programmable sharing — show the
          out-of-band link instead. */}
      {isLive ? (
        <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-border bg-bg-elevated text-[12px] text-muted">
          <Radio size={13} className="lucide-inline shrink-0 mt-0.5" />
          <span>
            Live collaborative doc — edits sync in real time, no version conflicts.{' '}
            {safeViewUrl ? (
              <a
                href={safeViewUrl}
                target="_blank"
                rel="noreferrer"
                className="text-accent hover:underline inline-flex items-center gap-0.5"
              >
                Manage sharing in {provLabel} <ExternalLink size={11} />
              </a>
            ) : (
              <span className="text-muted/60">Manage sharing in {provLabel}</span>
            )}
          </span>
        </div>
      ) : (
        <>
          <div className="flex flex-wrap gap-1.5 mb-2">
            {visOpts.map(({ value, label, icon: Icon, hint }) => (
              <button
                key={value}
                type="button"
                disabled={busy}
                onClick={() => {
                  // Confirm BEFORE mutating local state so cancelling PUBLIC leaves
                  // the selection in sync with the server (AutoSDE).
                  if (value === 'PUBLIC' && !window.confirm('Make this visible to everyone in your organization?')) {
                    return
                  }
                  setVisibility(value)
                  if (value === 'SHARED') return // reveal editor; commit via button
                  if (value === pub.visibility) return
                  // PRIVATE/PUBLIC apply immediately and never carry aliases.
                  sharingMut.mutate({ visibility: value, shared_with: [] })
                }}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] border transition-colors cursor-pointer disabled:opacity-40 ${
                  visibility === value
                    ? 'border-accent text-accent bg-accent-subtle'
                    : 'border-border text-muted hover:text-text hover:border-border-strong bg-transparent'
                }`}
                title={hint}
              >
                <Icon size={12} /> {label}
              </button>
            ))}
          </div>

          {/* Shared-with management — shown whenever Shared is the selected
              visibility (even before it is committed), so aliases can be added
              before the Share/Update call fires. */}
          {visibility === 'SHARED' && (
            <div className="mb-3">
              <AliasEditor
                aliases={aliases}
                aliasInput={aliasInput}
                setAliasInput={setAliasInput}
                addAlias={addAlias}
                removeAlias={removeAlias}
              />
              <Btn
                primary
                disabled={
                  busy ||
                  aliases.length === 0 ||
                  (pub.visibility === 'SHARED' &&
                    JSON.stringify(aliases) === JSON.stringify(pub.shared_with))
                }
                onClick={() => sharingMut.mutate({ visibility: 'SHARED', shared_with: aliases })}
                className="mt-1"
              >
                {sharingMut.isPending
                  ? 'Updating…'
                  : pub.visibility === 'SHARED'
                    ? 'Update access'
                    : 'Share'}
              </Btn>
            </div>
          )}
        </>
      )}

      {/* Version-sync status */}
      <div className="text-[12px] text-muted mb-3">
        {isLive ? (
          <>KiroCrew v{kiroV} · live in {provLabel}</>
        ) : (
          <>
            KiroCrew v{kiroV}
            {artiV ? <> → {provLabel} v{artiV}</> : null}
            {' · '}
            {pub.auto_sync ? 'auto-sync on' : 'auto-sync off'}
          </>
        )}
      </div>

      {error && <ShareError msg={error} />}

      <div className="flex items-center gap-2">
        {!isLive && (
          <Btn
            disabled={busy || pub.visibility === 'PRIVATE'}
            onClick={() => sharingMut.mutate({ visibility: 'PRIVATE', shared_with: [] })}
            title="Revoke sharing (set to Private)"
          >
            Un-share
          </Btn>
        )}
        <Btn
          danger
          disabled={busy}
          onClick={() => {
            if (window.confirm(`Remove from ${provLabel}? The share link will stop working.`)) {
              unpublishMut.mutate()
            }
          }}
          title={`Delete from ${provLabel} (keeps the local artifact)`}
        >
          <span className="inline-flex items-center gap-1">
            <Trash2 size={12} /> {unpublishMut.isPending ? 'Removing…' : 'Unpublish'}
          </span>
        </Btn>
      </div>
    </Card>
  )
}

function AliasEditor({
  aliases,
  aliasInput,
  setAliasInput,
  addAlias,
  removeAlias,
}: {
  aliases: string[]
  aliasInput: string
  setAliasInput: (v: string) => void
  addAlias: (v: string) => void
  removeAlias: (a: string) => void
}) {
  return (
    <div className="mb-3">
      <div className="flex flex-wrap items-center gap-1.5 mb-1.5">
        {aliases.map((a) => (
          <span
            key={a}
            className="inline-flex items-center gap-1 text-[12px] px-1.5 py-0.5 rounded bg-bg-elevated border border-border text-text"
          >
            {a}
            <button
              type="button"
              onClick={() => removeAlias(a)}
              className="hover:text-danger bg-transparent border-none cursor-pointer p-0 inline-flex items-center"
              aria-label={`Remove ${a}`}
            >
              <X size={10} />
            </button>
          </span>
        ))}
      </div>
      <Input
        value={aliasInput}
        onChange={(e) => setAliasInput(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ',' || e.key === ' ') {
            e.preventDefault()
            addAlias(aliasInput)
          }
        }}
        onBlur={() => addAlias(aliasInput)}
        placeholder="Add alias and press Enter…"
        aria-label="Add an alias to share with"
      />
    </div>
  )
}

function ShareError({ msg }: { msg: string }) {
  return (
    <div className="mb-3 px-3 py-2 rounded-md border border-danger/40 bg-danger-subtle text-[12px] text-danger">
      {msg}
    </div>
  )
}
