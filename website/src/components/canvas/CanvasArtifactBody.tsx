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
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import type { Artifact } from '../../types'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'
import CanvasEditor from './CanvasEditor'
import { parseCanvas, serializeCanvas, type Canvas } from './canvasModel'

/** How long after the last change a canvas saves. */
const SAVE_DELAY_MS = 700

type SaveState = 'saved' | 'pending' | 'saving' | 'conflict' | 'failed'

const isConflict = (err: unknown) => err instanceof ApiError && err.status === 409

export default function CanvasArtifactBody({ artifact, isCurrent }: { artifact: Artifact; isCurrent: boolean }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const content = artifact.content ?? ''
  const parsed = useMemo(() => parseCanvas(content), [content])
  const [canvas, setCanvas] = useState<Canvas | null>(parsed.ok ? parsed.canvas : null)
  const [state, setStateNow] = useState<SaveState>('saved')
  const [error, setError] = useState<string | null>(null)

  // What the server holds as far as this page knows: the bytes last read or
  // written, and the token that names them.
  const baseRef = useRef(content)
  const tokenRef = useRef(artifact.content_token)
  const draftRef = useRef<Canvas | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const inFlight = useRef(false)
  // The save loop reads the state between renders (after an await, from a
  // timer), so the ref moves in the same call as the state, never a render later.
  const stateRef = useRef<SaveState>('saved')
  const setState = useCallback((next: SaveState) => {
    stateRef.current = next
    setStateNow(next)
  }, [])

  // Content arriving from elsewhere (a reload, an agent's update, a version
  // picked) replaces the canvas, unless this page has edits of its own waiting:
  // those win until they are saved or the person chooses to reload.
  useEffect(() => {
    if (content === baseRef.current) return
    if (draftRef.current || inFlight.current) return
    baseRef.current = content
    tokenRef.current = artifact.content_token
    setCanvas(parsed.ok ? parsed.canvas : null)
    setError(null)
    setState('saved')
  }, [content, parsed, artifact.content_token, setState])

  const save = useCallback(async () => {
    timer.current = null
    const draft = draftRef.current
    if (!draft || inFlight.current || stateRef.current === 'conflict') return
    const body = serializeCanvas(draft)
    if (body === baseRef.current) { draftRef.current = null; setState('saved'); return }
    inFlight.current = true
    setState('saving')
    try {
      const saved = await api.updateArtifact(artifact.slug, { content: body, snapshot: false, expected_token: tokenRef.current })
      baseRef.current = body
      tokenRef.current = (saved as Artifact | undefined)?.content_token
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
      inFlight.current = false
    }
    // Changes made while that save was on the wire go out next. The cast is for
    // the compiler: the ref changed across the await, which narrowing cannot see.
    const now = stateRef.current as SaveState
    if (draftRef.current && now !== 'conflict' && now !== 'failed') {
      timer.current = setTimeout(() => { void save() }, SAVE_DELAY_MS)
    }
  }, [artifact.slug, qc, setState])

  const onChange = useCallback((next: Canvas) => {
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
      setCanvas(next.ok ? next.canvas : null)
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
        {/* No hand-off: nothing here is unsaved, but the canvas itself cannot be read, so there is nothing the agent could open. */}
        <ErrorNotice title={t('components.canvas.unreadable_title')} message={t('components.canvas.unreadable_message')} noHandoffReason="the stored document is not a canvas; the agent has no edit to recover" />
      </div>
    )
  }

  const status = state === 'saving' ? t('components.canvas.status_saving')
    : state === 'pending' ? t('components.canvas.status_pending')
      : state === 'saved' ? t('components.canvas.status_saved') : null

  return (
    <div className="flex flex-col gap-2">
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
        <CanvasEditor canvas={canvas} onChange={onChange} readOnly={!isCurrent} />
        {isCurrent && status && (
          <div data-testid="canvas-save-status" aria-live="polite"
            className="pointer-events-none absolute left-3 top-3 rounded-md bg-bg-elevated px-2 py-0.5 text-[11px] text-muted shadow-sm"
            style={{ zIndex: 9 }}>
            {status}
          </div>
        )}
        {!isCurrent && (
          <div data-testid="canvas-readonly-note"
            className="pointer-events-none absolute left-3 top-3 rounded-md bg-bg-elevated px-2 py-0.5 text-[11px] text-muted shadow-sm"
            style={{ zIndex: 9 }}>
            {t('components.canvas.past_version')}
          </div>
        )}
      </div>
    </div>
  )
}
