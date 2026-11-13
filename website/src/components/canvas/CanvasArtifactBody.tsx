/**
 * A canvas artifact on its detail page: the editor, always live, saving itself.
 *
 * A canvas has no edit mode. Every change is saved a moment after the last one
 * as a silent live save (`snapshot: false`), so dragging a sticky around never
 * adds a numbered version; the page's Snapshot control still takes one. Each
 * save carries the token of the content it was based on, so a canvas changed
 * elsewhere (another tab, an agent) is never overwritten: the save is refused,
 * autosave stops, and the person picks between reloading and keeping theirs.
 *
 * A past version, and a document that is not a canvas, open read-only. The
 * second matters: saving over content the editor cannot read would replace it
 * with an empty canvas.
 *
 * The page's own exits and its Snapshot reach these saves through
 * `canvasPending`: they flush what is waiting first, and ask before dropping
 * edits a failed or refused save left on this page.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import type { Artifact } from '../../types'
import { useConfirm } from '../ConfirmDialog'
import ErrorNotice from '../ErrorNotice'
import { usePublishNavigationStake, useRegisterNavigationLeaveGuard } from '../NavigationLeaveGuard'
import { Btn } from '../ui'
import CanvasEditor from './CanvasEditor'
import { parseCanvas, serializeCanvas, type Canvas } from './canvasModel'
import { registerCanvasPending } from './canvasPending'

/** How long after the last change a canvas saves. */
const SAVE_DELAY_MS = 700

type SaveState = 'saved' | 'pending' | 'saving' | 'conflict' | 'failed'

const isConflict = (err: unknown) => err instanceof ApiError && err.status === 409

const isOwnToken = (own: ReadonlySet<string>, token: string | undefined) => !!token && own.has(token)

export default function CanvasArtifactBody({ artifact, isCurrent }: { artifact: Artifact; isCurrent: boolean }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  // Every token this page's saves made current or replaced: each names content
  // no newer than what the page shows. The save loop and the sync read the ref
  // between renders; the state holds the same set, with the bytes the last save
  // wrote, for rendering. Reload clears both.
  const ownTokens = useRef(new Set<string>())
  const [own, setOwn] = useState<{ tokens: ReadonlySet<string>; body: string } | null>(null)
  // A masked read named by one of those tokens masks only bytes this page wrote
  // or replaced, so the page keeps showing the bytes its last save wrote instead.
  const ownMasked = isCurrent && !!artifact.content_redacted && !!artifact.content_token && !!own && own.tokens.has(artifact.content_token)
  const content = ownMasked ? own.body : artifact.content ?? ''
  // Every save names the token of the content it was based on, which is what
  // stops it overwriting a change made elsewhere. A canvas that mirrors a file
  // on disk has no token to name (the store compares nothing for it), so it
  // opens read-only rather than saving blind over the file. A canvas whose
  // shown content had text masked is read-only too: an autosave writes the whole
  // document, so it would put the placeholders over the text they hide.
  const redacted = !!artifact.content_redacted && !ownMasked
  // Set by Reload, which adopts a read of its own ahead of the page's copy.
  const [readRedacted, setReadRedacted] = useState(false)
  const editable = isCurrent && !!artifact.content_token && !redacted && !readRedacted
  const editableRef = useRef(editable)
  editableRef.current = editable
  const parsed = useMemo(() => parseCanvas(content), [content])
  const [canvas, setCanvas] = useState<Canvas | null>(parsed.ok ? parsed.canvas : null)
  const [state, setStateNow] = useState<SaveState>('saved')
  const [error, setError] = useState<string | null>(null)
  // Bumped whenever content from outside replaces the canvas, so the editor
  // drops an undo history that describes a document no longer on screen.
  const [epoch, setEpoch] = useState(0)
  const { confirm, confirmDialog } = useConfirm()

  // What the server holds as far as this page knows: the bytes last read or
  // written, and the token that names them.
  const baseRef = useRef(content)
  const tokenRef = useRef(artifact.content_token)
  const draftRef = useRef<Canvas | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const inFlight = useRef<Promise<void> | null>(null)
  // The save loop reads the state between renders (after an await, from a
  // timer), so the ref moves in the same call as the state, never a render later.
  const stateRef = useRef<SaveState>('saved')
  const setState = useCallback((next: SaveState) => {
    stateRef.current = next
    setStateNow(next)
  }, [])
  // The content the page is showing now, for a discard that happens between renders.
  const latest = useRef({ content, parsed, token: artifact.content_token })
  latest.current = { content, parsed, token: artifact.content_token }

  // Content arriving from elsewhere (a reload, an agent's update, a version
  // picked) replaces the canvas, unless this page has edits of its own waiting:
  // those win until they are saved or the person chooses to reload. The live
  // token is taken even when the bytes match, since a past version carries
  // none: coming back to Live from an identical version must not leave the next
  // save without one.
  // A token this page's own saves made or replaced is never taken back: a read
  // still carrying one predates the page's last save, and naming it would refuse
  // the next save as a conflict with the page's own write.
  useEffect(() => {
    if (draftRef.current || inFlight.current) return
    if (isCurrent && !isOwnToken(ownTokens.current, artifact.content_token)) tokenRef.current = artifact.content_token
    if (content === baseRef.current) return
    baseRef.current = content
    setCanvas(parsed.ok ? parsed.canvas : null)
    setEpoch(e => e + 1)
    setReadRedacted(false)
    setError(null)
    setState('saved')
  }, [content, parsed, artifact.content_token, isCurrent, setState])

  const save = useCallback((): Promise<void> => {
    timer.current = null
    if (inFlight.current) return inFlight.current
    const draft = draftRef.current
    if (!draft || stateRef.current === 'conflict') return Promise.resolve()
    const body = serializeCanvas(draft)
    if (body === baseRef.current) { draftRef.current = null; setState('saved'); return Promise.resolve() }
    setState('saving')
    const run = (async () => {
      try {
        const expected = tokenRef.current
        const saved = await api.updateArtifact(artifact.slug, { content: body, snapshot: false, expected_token: expected })
        baseRef.current = body
        const token = (saved as Artifact | undefined)?.content_token
        tokenRef.current = token
        if (token) {
          ownTokens.current.add(token)
          if (expected) ownTokens.current.add(expected)
          setOwn({ tokens: new Set(ownTokens.current), body })
        } else {
          ownTokens.current.clear()
          setOwn(null)
        }
        if (draftRef.current === draft) draftRef.current = null
        setError(null)
        setState(draftRef.current ? 'pending' : 'saved')
        void qc.invalidateQueries({ queryKey: ['artifact', artifact.slug] })
      } catch (err) {
        if (isConflict(err)) {
          setState('conflict')
        } else {
          setState('failed')
          setError(err instanceof Error ? err.message : String(err))
        }
      } finally {
        inFlight.current = null
      }
      // Changes made while that save was on the wire go out next. The cast is for
      // the compiler: the ref changed across the await, which narrowing cannot see.
      const now = stateRef.current as SaveState
      if (draftRef.current && now !== 'conflict' && now !== 'failed') {
        timer.current = setTimeout(() => { void save() }, SAVE_DELAY_MS)
      }
    })()
    inFlight.current = run
    return run
  }, [artifact.slug, qc, setState])

  /** Send what is waiting now. `true` once the server holds what this page shows. */
  const flush = useCallback(async (): Promise<boolean> => {
    // A save can find more edits waiting when it lands, so go round until none
    // are; the bound only stops a person typing through a flush from holding it.
    for (let round = 0; round < 4; round++) {
      if (timer.current) { clearTimeout(timer.current); timer.current = null }
      if (inFlight.current) { await inFlight.current; continue }
      if (!draftRef.current) return true
      if (stateRef.current === 'conflict') return false
      await save()
      const now = stateRef.current as SaveState
      if (now === 'conflict' || now === 'failed') return false
    }
    return !draftRef.current && !inFlight.current
  }, [save])

  const shown = useRef({ canvas, isCurrent })
  shown.current = { canvas, isCurrent }
  /** What the screen shows on the current version, waiting edits included. */
  const currentDocument = useCallback((): string | null => {
    const { canvas: now, isCurrent: live } = shown.current
    return live && now ? serializeCanvas(now) : null
  }, [])

  /** Drop this page's unsaved edits and show the content the page last read. */
  const discard = useCallback(() => {
    if (timer.current) { clearTimeout(timer.current); timer.current = null }
    draftRef.current = null
    const { content: now, parsed: read, token } = latest.current
    baseRef.current = now
    if (!isOwnToken(ownTokens.current, token)) tokenRef.current = token
    setCanvas(read.ok ? read.canvas : null)
    setEpoch(e => e + 1)
    setError(null)
    setState('saved')
  }, [setState])

  useEffect(() => registerCanvasPending(artifact.slug, { flush, discard, document: currentDocument }), [artifact.slug, flush, discard, currentDocument])

  // Leaving unmounts the only copy of edits the server does not hold yet, so an
  // exit waits for them. A waiting or in-flight save is finished here, while the
  // page still exists to show if it fails; the exit is refused meanwhile, and the
  // next one goes through once the save has landed. The guard cannot resume the
  // exit itself: the shell's registry does not pass its target. Edits a failed or
  // refused save left behind ask before they are dropped.
  usePublishNavigationStake(state !== 'saved')
  useRegisterNavigationLeaveGuard(() => {
    const now = stateRef.current
    if (now === 'saved' && !draftRef.current && !inFlight.current) return true
    if (now === 'pending' || now === 'saving') {
      void flush()
      return false
    }
    void confirm({
      title: t('pages.artifactDetailPage.discard_unsaved_changes'),
      confirmLabel: t('pages.artifactDetailPage.discard_changes_button'),
    }).then(ok => { if (ok) discard() })
    return false
  })

  const onChange = useCallback((next: Canvas) => {
    if (!editableRef.current) return
    setCanvas(next)
    draftRef.current = next
    if (stateRef.current === 'conflict') return
    setState('pending')
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => { void save() }, SAVE_DELAY_MS)
  }, [save, setState])

  // Leaving the page saves what is waiting rather than dropping it; closing the
  // tab with a save outstanding asks first.
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      if (draftRef.current) e.preventDefault()
    }
    window.addEventListener('beforeunload', warn)
    return () => {
      window.removeEventListener('beforeunload', warn)
      if (timer.current) { clearTimeout(timer.current); void save() }
    }
  }, [save])

  /** Drop this page's edits and show what the server holds now. */
  const reload = async () => {
    if (timer.current) clearTimeout(timer.current)
    try {
      const live = await api.artifact(artifact.slug) as Artifact
      const body = live.content ?? ''
      const next = parseCanvas(body)
      draftRef.current = null
      baseRef.current = body
      tokenRef.current = live.content_token
      ownTokens.current.clear()
      setOwn(null)
      // What was read may be masked before the page's own copy refreshes to say so.
      setReadRedacted(!!live.content_redacted)
      setCanvas(next.ok ? next.canvas : null)
      setEpoch(e => e + 1)
      setError(null)
      setState('saved')
    } catch (err) {
      setState('failed')
      setError(err instanceof Error ? err.message : String(err))
      return
    }
    void qc.invalidateQueries({ queryKey: ['artifact', artifact.slug] })
  }

  /** Keep this page's canvas: save it over whatever changed elsewhere. */
  const overwrite = async () => {
    try {
      const live = await api.artifact(artifact.slug) as Artifact
      tokenRef.current = live.content_token
      baseRef.current = live.content ?? ''
    } catch (err) {
      setState('failed')
      setError(err instanceof Error ? err.message : String(err))
      return
    }
    setState('pending')
    void save()
  }

  const retry = () => {
    setError(null)
    setState('pending')
    void save()
  }

  if (!canvas) {
    return (
      <div className="flex flex-col gap-3">
        {/* Nothing here is unsaved, so the agent can be asked to repair the stored document. */}
        <ErrorNotice title={t('components.canvas.unreadable_title')} message={t('components.canvas.unreadable_message')} askAgent />
      </div>
    )
  }

  const status = state === 'saving' ? t('components.canvas.status_saving')
    : state === 'pending' ? t('components.canvas.status_pending')
      : state === 'saved' ? t('components.canvas.status_saved') : null

  return (
    <div className="flex flex-col gap-2">
      {confirmDialog}
      {state === 'conflict' && (
        <ErrorNotice
          title={t('components.canvas.conflict_title')}
          message={t('components.canvas.conflict_message')}
          // No hand-off: this canvas holds edits that are not saved anywhere yet, and the hand-off navigates away.
          footer={(
            <div className="mt-2 flex gap-2">
              <Btn onClick={() => { void reload() }}>{t('components.canvas.conflict_reload')}</Btn>
              <Btn primary onClick={() => { void overwrite() }}>{t('components.canvas.conflict_keep_mine')}</Btn>
            </div>
          )}
        />
      )}
      {state === 'failed' && (
        <ErrorNotice
          title={t('components.canvas.save_failed_title')}
          message={error}
          // No hand-off: the failed save is still only on this page, and the hand-off navigates away.
          footer={<div className="mt-2"><Btn onClick={retry}>{t('components.canvas.save_retry')}</Btn></div>}
        />
      )}
      <div
        data-testid="canvas-artifact"
        className="relative overflow-hidden rounded-lg border border-border"
        style={{ height: 'max(420px, calc(100dvh - 220px))' }}
      >
        <CanvasEditor canvas={canvas} onChange={onChange} readOnly={!editable} epoch={epoch} />
        {editable && status && (
          <div data-testid="canvas-save-status" aria-live="polite"
            className="pointer-events-none absolute left-3 top-3 rounded-md bg-bg-elevated px-2 py-0.5 text-[11px] text-muted shadow-sm"
            style={{ zIndex: 9 }}>
            {status}
          </div>
        )}
        {!editable && (
          <div data-testid="canvas-readonly-note"
            className="pointer-events-none absolute left-3 top-3 rounded-md bg-bg-elevated px-2 py-0.5 text-[11px] text-muted shadow-sm"
            style={{ zIndex: 9 }}>
            {!isCurrent ? t('components.canvas.past_version') : redacted || readRedacted ? t('components.canvas.hidden_text') : t('components.canvas.linked_file')}
          </div>
        )}
      </div>
    </div>
  )
}
