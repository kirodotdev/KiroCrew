/**
 * The artifact identity a chat widget carries, shared by every way a widget is
 * drawn: the HTML frame (`WidgetFrame`) and the markdown-bodied widget
 * (`MarkdownWidget`). One hook resolves the slug, probes the artifact and
 * stars / unstars it; two small pieces draw what the hook knows -- the title
 * that links to the artifact page, and the star.
 *
 * Kept out of `WidgetFrame` so a widget that never builds an iframe still has
 * the same link and star: the backend registers EVERY emitted widget as an
 * artifact, whatever its body is.
 */
import { useState, useRef, useEffect, useMemo, useCallback } from 'react'
import { Star } from 'lucide-react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { IconButton } from './ui'
import ErrorNotice from './ErrorNotice'
import { effectiveWidgetSlug } from '../lib/widgetSlug'
import { api, ApiError } from '../api/client'
import { i18nT } from '../i18n/t'

/** Probe result for a widget's backing artifact.
 *
 * `exists` and `pinned` are SEPARATE states and must stay that way: the backend
 * auto-registers every emitted widget as an UNPINNED artifact, so the common case
 * is "exists, not in library" (hollow star) while still linking to the artifact. */
export interface WidgetArtifactState {
  exists: boolean
  pinned: boolean
}

/** Drop the cached session-scoped artifact lists so the in-session Artifacts tab
 * reflects a star/unstar immediately. No-op without a slot (embedded/detached
 * renders have no session to attribute to).
 *
 * `session-artifact-records` is the query that actually feeds widget rows, and
 * React Query prefix-matching does NOT reach it from `['artifacts']` -- omitting
 * it left the in-session tab showing a stale star. Keep all three in sync with
 * `useSessionArtifacts` / `useSessionArtifactRecords`. */
function invalidateSessionArtifacts(queryClient: QueryClient, slotKey: string | undefined): void {
  if (!slotKey) return
  queryClient.invalidateQueries({ queryKey: ['session-artifacts', slotKey] })
  queryClient.invalidateQueries({ queryKey: ['session-artifact-records', slotKey] })
  queryClient.invalidateQueries({ queryKey: ['artifacts'] })
}

export interface WidgetArtifactInputs {
  /** The widget body as emitted (inner HTML or markdown): the artifact's content
   *  and, with `messageTs`, the derived slug. */
  body: string
  /** The artifact's name when the star has to create it; the widget's own title. */
  title?: string
  /** Explicit slug attribute on `<mcwidget slug="...">`. */
  slug?: string
  messageTs?: string
  slotKey?: string
}

export interface WidgetArtifact {
  /** The slug this impression binds to, or null when neither an explicit slug nor
   *  a message context exists (streaming / detached widgets, test fixtures). */
  effectiveSlug: string | null
  /** The slug whenever the artifact EXISTS -- drives the title link. */
  existingSlug: string | null
  /** The slug when the artifact is starred -- drives the filled star. */
  savedSlug: string | null
  saving: boolean
  /** The last star / unstar failure. */
  saveError: string | null
  /** A probe failure other than 404 (a 404 is the benign no-artifact state): the
   *  widget cannot say whether its artifact exists, and the title row says so. */
  probeError: string | null
  /** Star when unstarred, unstar when starred. */
  toggleArtifact: () => Promise<void>
}

export function useWidgetArtifact({ body, title = 'Widget', slug, messageTs, slotKey }: WidgetArtifactInputs): WidgetArtifact {
  // Determine the effective slug for this impression. Priority:
  //  1. Explicit `slug` attribute from the agent (used when re-emitting a
  //     known saved artifact -- see artifacts skill).
  //  2. Derived from `messageTs + body`, so a slug hit proves the stored body
  //     equals this impression. Identical bodies in one message share a slug.
  const effectiveSlug = useMemo(
    () => effectiveWidgetSlug({ explicitSlug: slug, messageTs, body }),
    [slug, messageTs, body],
  )
  // Probe this widget's artifact. Cached via React Query with a 5-min
  // staleTime so repeated impressions / tab refocuses don't each fire a
  // network request. 404s are cached (not retried) to avoid a 404 storm.
  const queryClient = useQueryClient()
  const savedProbe = useQuery<WidgetArtifactState>({
    queryKey: ['artifact-saved', effectiveSlug],
    queryFn: async () => {
      try {
        const a = await api.artifact(effectiveSlug!)
        if (!a) return { exists: false, pinned: false }
        return { exists: true, pinned: !!(a as { pinned?: boolean }).pinned }
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { exists: false, pinned: false }
        throw e
      }
    },
    enabled: !!effectiveSlug,
    retry: false,
    staleTime: 5 * 60 * 1000,
    // Optimistic fill: an explicit slug attr means the agent re-emitted an
    // artifact it already knows about, so assume it exists and is starred
    // (the usual reason an agent carries a slug) until the probe resolves.
    placeholderData: slug ? { exists: true, pinned: true } : undefined,
  })
  const probeError = savedProbe.error
    ? (savedProbe.error instanceof Error ? savedProbe.error.message : String(savedProbe.error))
    : null
  const existingSlug = savedProbe.data?.exists ? effectiveSlug : null
  const savedSlug = savedProbe.data?.pinned ? effectiveSlug : null

  const [saveError, setSaveError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  // Track mount status so async save/remove callbacks can skip side-effects
  // when the component has been unmounted mid-flight.
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const saveAsArtifact = useCallback(async () => {
    if (saving || savedSlug || !effectiveSlug) return
    // Star = pin. The artifact itself normally already exists: the backend
    // auto-registers every emitted widget at message-finalize time. The create
    // below is the fallback for the cases where it doesn't -- a widget from
    // before this feature shipped, one whose registration failed, or one whose
    // record was reclaimed by the retention sweep. 409 means it raced into
    // existence between the probe and here, which is simply "already there".
    const name = title
    setSaving(true)
    setSaveError(null)
    try {
      if (!existingSlug) {
        try {
          await api.createArtifact({
            name,
            content: body,
            kind: 'widget',
            source: 'chat',
            slug: effectiveSlug,
            // Attribute it to the session it was starred from, so the
            // in-session Artifacts tab (a ?session= query) still finds a
            // fallback-created artifact.
            origin_session_key: slotKey || undefined,
          })
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 409)) throw e
        }
      }
      await api.setArtifactPinned(effectiveSlug, true)
      queryClient.setQueryData(['artifact-saved', effectiveSlug], { exists: true, pinned: true })
      invalidateSessionArtifacts(queryClient, slotKey)
    } catch (e) {
      if (mountedRef.current) {
        setSaveError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      if (mountedRef.current) setSaving(false)
    }
  }, [body, title, saving, savedSlug, existingSlug, effectiveSlug, slotKey, queryClient])

  const removeArtifact = useCallback(async () => {
    if (saving || !savedSlug) return
    // Un-star = unpin (metadata-only), NOT delete -- preserves the artifact and
    // its version history. The Artifacts library page handles permanent delete.
    setSaving(true)
    setSaveError(null)
    try {
      await api.setArtifactPinned(savedSlug, false)
      queryClient.setQueryData(['artifact-saved', effectiveSlug], { exists: true, pinned: false })
      invalidateSessionArtifacts(queryClient, slotKey)
    } catch (e) {
      // 404 -> the artifact is gone entirely (e.g. deleted from the library in
      // another tab); reconcile to not-exists, not merely not-pinned.
      if (e instanceof ApiError && e.status === 404) {
        queryClient.setQueryData(['artifact-saved', effectiveSlug], { exists: false, pinned: false })
        invalidateSessionArtifacts(queryClient, slotKey)
      } else if (mountedRef.current) {
        setSaveError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      if (mountedRef.current) setSaving(false)
    }
  }, [savedSlug, saving, effectiveSlug, slotKey, queryClient])

  return {
    effectiveSlug,
    existingSlug,
    savedSlug,
    saving,
    saveError,
    probeError,
    toggleArtifact: savedSlug ? removeArtifact : saveAsArtifact,
  }
}

/** The widget's title, linked whenever the artifact EXISTS -- not only when
 *  starred -- plus the save-failed note. */
export function WidgetArtifactTitle({ title, artifact, className }: {
  title: string
  artifact: WidgetArtifact
  className?: string
}) {
  return (
    <span className={className ?? 'text-[13px] font-medium text-text truncate'}>
      {artifact.existingSlug ? (
        <a
          href={`/artifacts/${artifact.existingSlug}`}
          className="text-text hover:text-accent hover:underline"
          title={i18nT('components.widgetFrame.open_artifact_named', { slug: artifact.existingSlug })}
        >{title}</a>
      ) : (
        title
      )}
      {/* A star failure, else a probe failure: both read with the agent hand-off. A
          failed star or read leaves nothing to destroy, so the hand-off is safe. */}
      {(artifact.saveError || artifact.probeError) && (
        <ErrorNotice
          variant="inline"
          askAgent
          className="ml-2 min-w-0"
          title={artifact.saveError ? i18nT('components.widgetFrame.save_failed') : undefined}
          message={artifact.saveError ?? artifact.probeError}
        />
      )}
    </span>
  )
}

/** The star: filled while the artifact is in the library, hollow otherwise. */
export function WidgetStarButton({ artifact }: { artifact: WidgetArtifact }) {
  const { effectiveSlug, savedSlug, saving, toggleArtifact } = artifact
  return (
    <IconButton
      variant={savedSlug ? 'active' : 'default'}
      onClick={toggleArtifact}
      disabled={saving || !effectiveSlug}
      className={saving ? 'cursor-wait' : ''}
      title={
        !effectiveSlug
          ? i18nT('components.widgetFrame.cannot_star_widget_has_no_slug_or_message_contex')
          : savedSlug
            ? i18nT('components.widgetFrame.starred_as_click_to_remove', { name: savedSlug })
            : i18nT('components.widgetFrame.star_as_artifact')
      }
      aria-label={savedSlug ? i18nT('components.widgetFrame.remove_artifact_from_library', { name: savedSlug }) : i18nT('components.widgetFrame.star_as_artifact')}
    >
      <Star size={12} fill={savedSlug ? 'currentColor' : 'none'} />
    </IconButton>
  )
}
