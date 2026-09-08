import { lazy, Suspense, useCallback, useEffect, useId, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion, useReducedMotion } from 'framer-motion'
import { Share2, X } from 'lucide-react'

import { api, type FeatureVideo, type FeatureVideoNext } from '../api/client'
import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { tipDocHref } from '../utils/docsLink'
import { i18nT } from '../i18n/t'
import { useAppSelector } from '../store'
import { recordError } from '../utils/errorReport'
import { markStartupVideoHandled } from './startupVideoGate'

/**
 * The feature-intro video shown once at startup.
 *
 * Mounted only after `startupVideoGate` has ruled that this launch is free — that
 * module owns "may anything open at all", this file owns "what is it and what
 * does watching it mean". The split is what keeps the policy testable without the
 * dashboard and keeps this chunk lazy.
 *
 * The clip retires PERMANENTLY, by one of two verdicts, and both are one-way:
 * `seen` when the user watches most of it or presses the acknowledgement, and
 * `dismissed` when they close it. Neither is a snooze — the backend stops
 * offering a clip after either — so there is no "remind me" affordance here to
 * imply otherwise.
 *
 * The clip's BYTES are not fetched until the user asks for them: `preload="none"`
 * plus a `poster` means the browser draws the still and waits for a play. A
 * launch that shows no video (the overwhelming majority) costs one small JSON
 * response and no media at all.
 *
 * The one launch that WOULD show a video pays one more small request first: a
 * same-origin HEAD on the clip. The backend already refuses to offer a clip whose
 * file it cannot see on disk -- that is the primary defence -- and this probe is
 * the belt to those braces, for the file that vanishes or the static route that
 * breaks between the catalog check and the render. Under `preload="none"` the
 * player's own `onError` cannot fire until the user presses play, so without the
 * probe an unreachable clip opens a dialog with a still that plays nothing.
 */

const LazyShareMessageModal = lazy(() => import('../pages/chat/share/ShareMessageModal'))

/** Fraction of the clip that counts as watched. */
const SEEN_AT = 0.8

export interface StartupVideoModalProps {
  /**
   * The `capabilities.social_share` governance answer, as `/api/dashboard/config`
   * reports it in `social_share_enabled`.
   *
   * Defaults to FALSE so a caller that forgets to wire it hides sharing rather
   * than exposing it — the same fail-closed posture as `AssistantMessage`. This
   * component adds no scope and no flag of its own: sharing a feature clip is the
   * same act, under the same policy, as sharing a reply.
   */
  shareEnabled?: boolean
  /** Called once a verdict has been dispatched and the modal should go away. */
  onClose: () => void
}

export default function StartupVideoModal({ shareEnabled = false, onClose }: StartupVideoModalProps) {
  // The active slot's key rides both requests so the server's restricted-session
  // guard sees the REAL session rather than the shared `dashboard:ui` default,
  // which it treats as unrestricted. Without it the server cannot refuse a
  // PERMANENT verdict from a session that keeps nothing, and the dashboard's own
  // gate is the only thing left -- the same reason `MobileLoginCard` passes it.
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const sessionKey = activeSlot ? `dashboard:${activeSlot}` : undefined

  const { data, isError } = useQuery<FeatureVideoNext>({
    // NOT in the query key: the answer is instance-wide, and re-fetching per slot
    // would break "one request per launch".
    queryKey: ['feature-video-next'],
    queryFn: () => api.featureVideoNext(sessionKey),
    // One request per launch. The answer cannot change underneath us: only this
    // component's own verdict retires a clip, and by then it is closing.
    staleTime: Infinity,
    gcTime: Infinity,
    // A 404 is the expected answer from a gateway that predates the endpoint, and
    // asking again recovers nothing — so no retry, and the error path renders
    // nothing rather than an empty dialog.
    retry: false,
  })

  const video = data?.video ?? null
  const offered = !isError && !!video && data?.enabled === true

  /**
   * Is the clip actually THERE? `'idle'` until the gate says the dialog would
   * open, then one same-origin HEAD on `video.src` decides `'ok'` or `'failed'`.
   *
   * The dialog renders on `'ok'` only. `'failed'` closes without a verdict, the
   * same contract as `onMediaError` below: a 404 on the clip is not the user
   * deciding anything about it, and `dismissed` is permanent.
   *
   * ONE probe per mount, held in a ref rather than derived from state, so the
   * effect cannot re-issue it on a re-render -- and, under StrictMode's doubled
   * effect run, the second pass finds the first already in flight and leaves it.
   * The probe is deliberately NOT cancelled on cleanup for the same reason: the
   * doubled run's cleanup would otherwise drop the only result that will ever
   * come, and the modal would sit at `'idle'` forever.
   *
   * `credentials: 'same-origin'` is what the `<video>` element itself would send
   * for a same-origin `src`, so the probe sees the same answer the player would.
   * `src` is same-origin by contract -- the backend's validator only ever names a
   * path under `/app-assets/feature-videos/` -- and this probe relies on that
   * rather than relaxing it.
   */
  const [probe, setProbe] = useState<'idle' | 'ok' | 'failed'>('idle')
  const probeStarted = useRef(false)

  /**
   * The clip is not reachable. Journal it and close, exactly as `onMediaError`
   * does for a mid-playback failure -- same `source`, same message, same
   * endpoint -- so a reader of the error journal sees one story for "the clip
   * did not load" with the HTTP status (or the network reason) telling which
   * half it came from. No verdict of either kind: the clip is offered again next
   * launch, once its file is back.
   *
   * `markStartupVideoHandled` is what the App calls when it mounts this modal,
   * and it is idempotent, so for the normal path this is a no-op. It is here so
   * that ANY host of this component -- not only the App gate -- spends the launch
   * on a failed probe rather than letting a re-mount retry it.
   */
  const failProbe = useCallback((src: string, status: number | undefined, detail: string | undefined) => {
    recordError({
      source: 'api',
      message: i18nT('components.startupVideoModal.media_failed'),
      endpoint: src,
      status,
      detail,
    })
    markStartupVideoHandled()
    setProbe('failed')
    onClose()
  }, [onClose])

  useEffect(() => {
    if (!offered || !video || probeStarted.current) return
    probeStarted.current = true
    const src = video.src
    fetch(src, { method: 'HEAD', credentials: 'same-origin' }).then(
      res => {
        if (res.ok) setProbe('ok')
        else failProbe(src, res.status, undefined)
      },
      (err: unknown) => {
        failProbe(src, undefined, err instanceof Error ? err.message : undefined)
      },
    )
  }, [offered, video, failProbe])

  // Nothing is on screen until there is a clip AND the feature is on AND the
  // probe has seen the clip, so the dialog itself lives in its own component
  // below. That component MOUNTS at the moment the dialog appears, which is what
  // the shared focus trap needs: the trap moves focus in on ITS mount and never
  // re-runs, so a trap mounted here — while this component still returns null —
  // would aim at a dialog that does not exist yet and leave focus on the page
  // behind the overlay for good.
  if (!offered || !video || probe !== 'ok') return null

  return (
    <OpenStartupVideoModal
      video={video}
      shareEnabled={shareEnabled}
      onClose={onClose}
      sessionKey={sessionKey}
    />
  )
}

/** The dialog itself. Mounted only while there is a clip to show. */
function OpenStartupVideoModal({ video, shareEnabled, onClose, sessionKey }: {
  video: FeatureVideo
  shareEnabled: boolean
  onClose: () => void
  sessionKey: string | undefined
}) {
  const reduceMotion = useReducedMotion()
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const reactId = useId()
  const titleId = `${reactId}-title`
  const descId = `${reactId}-desc`
  const [shareOpen, setShareOpen] = useState(false)
  /** One verdict per clip. `timeupdate` fires several times a second, and the
   *  user can still press a button after crossing the threshold, so without this
   *  the same decision is posted repeatedly. */
  const verdictSent = useRef(false)

  /**
   * Record the verdict. Does NOT close -- that separation is the whole point.
   *
   * Crossing the watched threshold is a fact about the clip, not a request to take it
   * off screen. Closing here snatched the dialog away mid-playback and, because the
   * verdict is permanent, made the final stretch unwatchable for good.
   *
   * Deliberately not awaited: nothing the user does next should wait on this write.
   * The rejection is NOT discarded either -- `api/client.ts`'s `j` helper calls
   * `recordError` on every non-2xx, so a refused verdict is already in the error
   * journal, with its endpoint, status and backend code, before this handler runs.
   * The `catch` exists only to keep that expected rejection from surfacing as an
   * unhandled one; the journal is the record.
   */
  const recordVerdict = useCallback((status: 'seen' | 'dismissed') => {
    if (verdictSent.current) return
    verdictSent.current = true
    void api.featureVideoFeedback(video.id, status, sessionKey).catch(() => {})
  }, [video, sessionKey])

  /**
   * Record the verdict AND close -- for the moments the user is actually done: the
   * clip ended, or they pressed something. A verdict already recorded at the 80% mark
   * wins, so finishing a clip you acknowledged early does not post twice.
   */
  const settle = useCallback((status: 'seen' | 'dismissed') => {
    recordVerdict(status)
    onClose()
  }, [recordVerdict, onClose])

  const dismiss = useCallback(() => settle('dismissed'), [settle])

  /**
   * Close and record NOTHING -- the backdrop's behaviour.
   *
   * A stray click on the scrim is not a decision about the clip, and `dismissed` is
   * permanent: one misclick used to retire a clip the user never watched, with no way
   * back. Closing silently leaves the verdict unwritten, so the backend offers it
   * again next launch. A verdict already recorded at the 80% mark is untouched -- this
   * only declines to write a NEW one.
   */
  const closeWithoutVerdict = useCallback(() => { onClose() }, [onClose])

  // Focus in, focus restore on close, Escape, and the Tab/Shift+Tab trap — the
  // shared implementation the other hand-rolled dialogs use. Suspended while the
  // share dialog is up so one Escape does not close both layers.
  useDialogFocusTrap(dialogRef, dismiss, { enabled: !shareOpen })

  const onTimeUpdate = (e: React.SyntheticEvent<HTMLVideoElement>) => {
    const el = e.currentTarget
    // Prefer the element's own metadata, and fall back to the catalog duration for
    // the frames before it loads (and for a source whose duration never resolves).
    const total = Number.isFinite(el.duration) && el.duration > 0 ? el.duration : video.duration_s
    if (!total || !Number.isFinite(total)) return
    // Record only. The clip keeps playing and the dialog stays put -- the user decides
    // when it goes away.
    if (el.currentTime / total >= SEEN_AT) recordVerdict('seen')
  }

  /**
   * The clip's bytes did not load mid-playback -- a file that vanished after the
   * HEAD probe above passed, or a codec this browser cannot play. (The probe is
   * the first layer, for a clip that is not there at all; this is the second,
   * for what a HEAD cannot tell.)
   *
   * Close, and record NO verdict. An empty player is the one thing worse than no
   * dialog: the user is handed a control that cannot do anything, for a feature
   * they never asked about. Staying silent about the verdict is what gives the
   * clip a next launch -- `dismissed` is permanent, so writing it here would
   * retire a clip the user was never actually shown, exactly the misclick case
   * the backdrop already avoids.
   *
   * The failure is journaled rather than logged. Nothing else records it: the
   * browser fetches `src` itself, so `api/client.ts`'s `j` helper never sees this
   * request and `recordError` is the only path to the error journal. `source` is
   * `'api'` because a same-origin GET really did fail and `endpoint` names it --
   * `'system'` is for a subsystem reported broken inside a response that
   * SUCCEEDED, which is the opposite of this. A media `error` event carries no
   * HTTP status, so `status` is left unset and the `MediaError` code goes in
   * `detail`, where a reader can tell "not found" from "cannot decode".
   */
  const onMediaError = (e: React.SyntheticEvent<HTMLVideoElement>) => {
    const err = e.currentTarget.error
    recordError({
      source: 'api',
      message: i18nT('components.startupVideoModal.media_failed'),
      endpoint: video.src,
      // `MediaError.code` tells "not found" from "cannot decode", which is the whole
      // diagnostic value here, and it is a machine-readable classification -- the
      // field's purpose -- rather than prose. `detail` carries the browser's OWN
      // message and nothing authored, the same way the error boundaries forward
      // `error.message`.
      code: err ? String(err.code) : undefined,
      detail: err?.message || undefined,
    })
    closeWithoutVerdict()
  }

  // Share caption: the feature's own words plus a link to its docs page, so a
  // reader of the post can go and read more. No authored copy either way -- the
  // blurb comes from the API and the URL from a shared helper.
  //
  // `video.doc` is a bare docs FILENAME in the catalog (`"feature-tips.md"`), not a
  // URL, and unlike `tipsNext` no resolved `doc_link` ships beside it. `tipDocHref`
  // is the existing, validated resolver for exactly that field -- it refuses
  // anything that is not a plain `*.md` filename and returns null, so a bad catalog
  // entry drops the link instead of pasting a filename nobody can open.
  const shareBody = [video.description, tipDocHref(video.doc)].filter(Boolean).join('\n\n')
  // The post text: title first so a reader knows what the clip is, then the
  // same blurb and link the card shows. One paragraph, because the X composer
  // treats it as a post, not a document. The user can still edit it in the
  // dialog before anything is sent.
  const shareCaption = [video.title, shareBody].filter(Boolean).join('\n\n')

  return (
    // Presentation, not a control: an ARIA button may not contain interactive
    // descendants, and focus never lands on the scrim, so a keydown handler here
    // would be unreachable. Escape covers keyboard dismissal -- and unlike this
    // scrim it RECORDS a verdict, because pressing it is a deliberate act where a
    // stray click is not.
    <div
      className="fixed inset-0 z-50 bg-bg/80 backdrop-blur-sm flex items-center justify-center"
      role="presentation"
      onClick={e => { if (e.target === e.currentTarget) closeWithoutVerdict() }}
    >
      <motion.div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        // Named by its own heading and described by the blurb, so the clip's real
        // title is announced rather than a generic label.
        aria-labelledby={titleId}
        aria-describedby={descId}
        // `initial={false}` is framer-motion's own way to say "start where you
        // end": under reduce-motion the dialog is simply present, with no
        // entrance to sit through.
        initial={reduceMotion ? false : { opacity: 0, y: 8, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={reduceMotion ? { duration: 0 } : { duration: 0.22, ease: 'easeOut' }}
        className="bg-card border border-border rounded-xl shadow-xl w-[560px] max-w-[92vw] flex flex-col overflow-hidden outline-none"
      >
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-border bg-bg-elevated">
          <span className="text-sm font-semibold text-text">
            {i18nT('components.startupVideoModal.feature_intro')}
          </span>
          <button
            type="button"
            className="text-muted hover:text-text cursor-pointer bg-transparent border-none"
            onClick={dismiss}
            aria-label={i18nT('components.startupVideoModal.close')}
          >
            <X size={16} />
          </button>
        </div>

        {/* eslint-disable-next-line jsx-a11y/media-has-caption -- the contract
            (`GET /api/feature-videos/next`) carries no captions track, and the
            placeholder clip has no audio to caption. A narrated clip DOES need
            one, so the field is a known gap recorded on the PR rather than a
            guessed-at addition to the API. */}
        <video
          data-testid="startup-video"
          className="w-full bg-bg aspect-video"
          src={video.src}
          poster={video.poster}
          // Names the player after the clip it plays, so the control is not an
          // unlabelled surface in the tab order.
          aria-label={video.title}
          controls
          playsInline
          // No autoplay and no preload: the bytes arrive when the user asks for
          // them, so an unwatched clip costs nothing beyond the poster.
          preload="none"
          onTimeUpdate={onTimeUpdate}
          onEnded={() => settle('seen')}
          onError={onMediaError}
        />

        <div className="px-4 py-3 text-sm text-text">
          <p id={titleId} className="font-semibold text-text-strong">{video.title}</p>
          <p id={descId} className="mt-1 text-[13px] text-muted">{video.description}</p>
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2 px-4 py-2.5 border-t border-border bg-bg-elevated">
          {/* Governance is a RENDER gate here, not a disabled state: an entry the
              policy has not granted should not be on screen at all, so there is no
              greyed button to explain and nothing on the page that could reach an
              intent URL. */}
          {shareEnabled && (
            <button
              type="button"
              data-testid="startup-video-share"
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-border text-text hover:border-border-strong bg-transparent cursor-pointer"
              onClick={() => setShareOpen(true)}
            >
              <Share2 size={14} className="lucide-inline" />
              {i18nT('components.startupVideoModal.share')}
            </button>
          )}
          <button
            type="button"
            className="px-3 py-1.5 text-sm rounded-md bg-accent text-accent-fg hover:opacity-90 cursor-pointer"
            onClick={() => settle('seen')}
          >
            {i18nT('components.startupVideoModal.got_it')}
          </button>
        </div>
      </motion.div>

      {/* The existing chat share card, reused whole: the clip's title takes the
          question slot and its blurb plus doc link the excerpt slot, which is the
          shape that card already renders.
          Gated on `shareOpen` ALONE, matching `AssistantMessage.tsx`. The card
          guards itself: it re-reads permission from a ref AFTER its export await,
          and that ref only refreshes while the card keeps rendering. Unmounting it
          on a revoked policy freezes the ref at `true`, and an export already in
          flight then opens the social composer anyway — the very navigation the
          revocation exists to stop. So the card stays and is handed the live
          answer, which it uses to show a notice and withdraw its own actions.
          Fail-closed still holds at the ENTRY: the Share button above is gated, so
          no new share can START once the policy says no. */}
      {shareOpen && (
        <Suspense fallback={null}>
          <LazyShareMessageModal
            onClose={() => setShareOpen(false)}
            messageText={shareBody}
            prevUserText={video.title}
            shareEnabled={shareEnabled}
            // The card's own copy describes a chat reply and a question. Here the
            // subject is a feature clip and its title, so the two strings that name
            // the shared thing are replaced. Everything else in that dialog is about
            // the sharing mechanics and reads correctly as-is.
            copy={{
              description: i18nT('components.startupVideoModal.share_description'),
              includeQuestion: i18nT('components.startupVideoModal.share_include_title'),
              // The POST text, not only the card. The card's default caption says
              // the assistant "just did this for me", which is about a reply; a
              // feature clip did nothing for anyone. The social composers and
              // the clipboard receive `caption`, so without this the blurb and
              // the docs link only ever reached the image, and the post itself
              // carried the wrong sentence.
              caption: shareCaption,
            }}
          />
        </Suspense>
      )}
    </div>
  )
}
