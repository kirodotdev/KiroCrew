/**
 * The registered-action guide's on-screen layer: one small control pill and,
 * while a step's target is found, an arrow and outline on that control.
 *
 * Non-modal by construction. There is no scrim and no full-screen element:
 * the arrow and outline are `pointer-events: none` and hidden from assistive
 * tech, so every click and key still reaches the page underneath, and only the
 * pill's own buttons take input. The pill never takes focus on its own.
 */
import { useState } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import { motion, useReducedMotion } from 'framer-motion'
import { ArrowDown, ArrowUp, Compass, Loader2, X } from 'lucide-react'
import { Btn, IconButton } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import type { GuideActionRefusal } from './guideActions'
import { useGuide, type GuideView } from './GuideContext'
import { useGuideStepTracker, type GuideRect } from './useGuideStepTracker'

const ARROW = 24
const GAP = 6

/** Where the arrow sits for a target: above it, or below when there is no room. */
export function arrowPlacement(rect: GuideRect, viewport: { width: number; height: number }): { top: number; left: number; up: boolean } {
  const up = rect.top < ARROW + GAP + 8
  const top = up ? rect.top + rect.height + GAP : rect.top - ARROW - GAP
  const center = rect.left + rect.width / 2 - ARROW / 2
  const left = Math.min(Math.max(center, 4), Math.max(4, viewport.width - ARROW - 4))
  return { top, left, up }
}

function GuideArrow({ rect, reduceMotion }: { rect: GuideRect; reduceMotion: boolean }) {
  const place = arrowPlacement(rect, { width: window.innerWidth, height: window.innerHeight })
  const Icon = place.up ? ArrowUp : ArrowDown
  return (
    <>
      <div
        aria-hidden="true"
        data-testid="guide-target-outline"
        className="pointer-events-none fixed z-[10002] rounded-md border-2 border-accent"
        style={{ top: rect.top - 4, left: rect.left - 4, width: rect.width + 8, height: rect.height + 8 }}
      />
      <motion.div
        aria-hidden="true"
        data-testid="guide-arrow"
        className="pointer-events-none fixed z-[10002] text-accent"
        style={{ top: place.top, left: place.left, width: ARROW, height: ARROW }}
        animate={reduceMotion ? undefined : { y: place.up ? [0, 4, 0] : [0, -4, 0] }}
        transition={reduceMotion ? undefined : { duration: 1.2, repeat: Infinity, ease: 'easeInOut' }}
      >
        <Icon size={ARROW} strokeWidth={2.5} />
      </motion.div>
    </>
  )
}

const REFUSAL_KEYS: Record<GuideActionRefusal, string> = {
  unknown_action: 'components.guideLayer.refused_unknown_action',
  invalid_params: 'components.guideLayer.refused_invalid_params',
  unknown_setting: 'components.guideLayer.refused_unknown_setting',
  sensitive_setting: 'components.guideLayer.refused_sensitive_setting',
}
const FINISHED_KEYS: Record<string, string> = {
  completed: 'components.guideLayer.finished_completed',
  cancelled: 'components.guideLayer.finished_cancelled',
  expired: 'components.guideLayer.finished_expired',
}

function Pill({ children, label }: { children: React.ReactNode; label: string }) {
  return (
    <div
      role="region"
      aria-label={label}
      data-testid="guide-pill"
      className="relative z-20 mx-2 mb-2 flex shrink-0 flex-col gap-2 rounded-xl border border-border bg-card px-3 py-2 text-[13px] text-text"
    >
      {children}
    </div>
  )
}

/**
 * `source` names who proposed the guide. The pill floats over whatever page is
 * open, so an action title alone ("Show the setting …") reads as an
 * unattributed banner offering to drive the page.
 */
function PillHead({ title, source, onClose, closeLabel }: { title: string; source?: string; onClose?: () => void; closeLabel: string }) {
  return (
    <div className="flex items-start gap-2">
      <Compass size={16} className="mt-0.5 shrink-0 text-accent" aria-hidden="true" />
      <span className="min-w-0 flex-1">
        {source && <span className="block text-[11.5px] text-muted" data-testid="guide-source">{source}</span>}
        <span className="block font-semibold text-text-strong">{title}</span>
      </span>
      {onClose && (
        <IconButton aria-label={closeLabel} title={closeLabel} onClick={onClose} className="shrink-0">
          <X size={14} aria-hidden="true" />
        </IconButton>
      )}
    </div>
  )
}

function ActiveStep({ view, rect }: { view: GuideView; rect: GuideRect | null }) {
  const { t } = useTranslation()
  const ctx = useGuide()!
  const step = view.step!
  const waiting = step.complete.kind === 'committed' && ctx.submitted && !rect
  return (
    <>
      <p className="m-0" aria-live="polite" data-testid="guide-step-text">
        {waiting ? t('components.guideLayer.waiting_for_confirmation') : t(step.textKey)}
      </p>
      {!rect && !waiting && (
        <p className="m-0 flex items-center gap-1.5 text-muted" role="status">
          <Loader2 size={13} className="animate-spin" aria-hidden="true" /> {t('components.guideLayer.looking_for_control')}
        </p>
      )}
      {ctx.submitted && <p className="m-0 text-[12px] text-muted">{t('components.guideLayer.cancel_does_not_undo')}</p>}
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Btn onClick={ctx.cancel} disabled={ctx.busy}>{t('components.guideLayer.cancel_guide')}</Btn>
        {step.complete.kind === 'ack' && (
          <Btn primary onClick={() => ctx.report('observed')} disabled={!rect} data-testid="guide-next">
            {t('components.guideLayer.next')}
          </Btn>
        )}
      </div>
    </>
  )
}

export default function GuideLayer() {
  const { t } = useTranslation()
  const ctx = useGuide()
  const reduceMotion = !!useReducedMotion()
  const view = ctx?.view ?? null
  const tracking = !!view && view.ownedHere && !view.needsEnter
    && (view.guide.status === 'active' || view.guide.status === 'target_missing') && !!view.step
  const stepId = view ? `${view.guide.guide_id}:${view.guide.action_index}:${view.guide.step_index}` : ''
  const rect = useGuideStepTracker({
    stepId,
    step: view?.step ?? null,
    enabled: tracking,
    suppressMissing: !!ctx?.submitted,
    reduceMotion,
    onObserved: () => ctx?.report('observed'),
    onMissing: () => ctx?.report('target_missing'),
  })
  const [hiddenPendingError, setHiddenPendingError] = useState<string | null>(null)
  if (!ctx) return null

  const label = t('components.guideLayer.region_label')
  const closeLabel = t('components.guideLayer.close')
  const error = (
    <>
      {/* No hand-off: the guide pill floats over the page being guided, whose unsaved form draft the hand-off navigation would discard. */}
      <ErrorNotice variant="inline" className="text-[12px]" message={ctx.error} testId="guide-error" />
    </>
  )

  let body: React.ReactNode = null
  if (!view && !ctx.finished && ctx.pendingError && ctx.pendingError !== hiddenPendingError) {
    body = (
      <Pill label={label}>
        <PillHead title={t('components.guideLayer.title_generic')} onClose={() => setHiddenPendingError(ctx.pendingError)} closeLabel={closeLabel} />
        {/* No hand-off: the pill floats over whatever page is open, whose unsaved draft the hand-off navigation would discard. */}
        <ErrorNotice variant="inline" className="text-[12px]" message={ctx.pendingError} testId="guide-pending-error" />
      </Pill>
    )
  } else if (!view && ctx.finished) {
    body = (
      <Pill label={label}>
        <PillHead title={t(FINISHED_KEYS[ctx.finished.status] ?? 'components.guideLayer.finished_cancelled')} onClose={ctx.dismissFinished} closeLabel={closeLabel} />
        {ctx.finished.actions.map((action, index) => {
          const result = action.result
          if (!result) return null
          return action.id === 'crewmate.create' && typeof result.name === 'string'
            ? <p key={index} className="m-0">{t('components.meetCrewmatesFlow.step4_title', { name: result.name })}</p>
            : null
        })}
      </Pill>
    )
  } else if (view) {
    const title = view.action ? t(view.action.titleKey, view.action.titleVars) : t('components.guideLayer.title_generic')
    const source = view.action ? t('components.guideLayer.title_generic') : undefined
    const g = view.guide
    if (!view.resolved.ok) {
      body = (
        <Pill label={label}>
          <PillHead title={t('components.guideLayer.title_generic')} closeLabel={closeLabel} />
          <p className="m-0">{t(REFUSAL_KEYS[view.resolved.reason])}</p>
          {error}
          <div className="flex justify-end"><Btn onClick={ctx.cancel} disabled={ctx.busy}>{t('components.guideLayer.dismiss')}</Btn></div>
        </Pill>
      )
    } else if (g.status === 'target_missing') {
      body = (
        <Pill label={label}>
          <PillHead title={title} source={source} closeLabel={closeLabel} />
          <p className="m-0" role="status">{t('components.guideLayer.target_missing')}</p>
          {error}
          <div className="flex justify-end"><Btn onClick={ctx.cancel} disabled={ctx.busy}>{t('components.guideLayer.cancel_guide')}</Btn></div>
        </Pill>
      )
    } else if (g.status === 'offered') {
      body = (
        <Pill label={label}>
          <PillHead title={title} source={source} closeLabel={closeLabel} />
          <p className="m-0 text-[12px] text-muted">{t('components.guideLayer.offer_hint')}</p>
          {error}
          <div className="flex flex-wrap justify-end gap-2">
            <Btn onClick={ctx.cancel} disabled={ctx.busy}>{t('components.guideLayer.dismiss')}</Btn>
            <Btn primary onClick={ctx.start} disabled={ctx.busy} data-testid="guide-start">{t('components.guideLayer.start')}</Btn>
          </div>
        </Pill>
      )
    } else if (!view.ownedHere) {
      body = (
        <Pill label={label}>
          <PillHead title={title} source={source} closeLabel={closeLabel} />
          <p className="m-0">{t('components.guideLayer.other_tab')}</p>
          {error}
          <div className="flex flex-wrap justify-end gap-2">
            <Btn onClick={ctx.cancel} disabled={ctx.busy}>{t('components.guideLayer.cancel_guide')}</Btn>
            <Btn primary onClick={ctx.takeOver} disabled={ctx.busy} data-testid="guide-take-over">{t('components.guideLayer.take_over')}</Btn>
          </div>
        </Pill>
      )
    } else if (view.needsEnter) {
      body = (
        <Pill label={label}>
          <PillHead title={title} source={source} closeLabel={closeLabel} />
          <p className="m-0">{t('components.guideLayer.continue_hint')}</p>
          {error}
          <div className="flex flex-wrap justify-end gap-2">
            <Btn onClick={ctx.cancel} disabled={ctx.busy}>{t('components.guideLayer.cancel_guide')}</Btn>
            <Btn primary onClick={ctx.continueAction} disabled={ctx.busy} data-testid="guide-continue">{t('components.guideLayer.continue')}</Btn>
          </div>
        </Pill>
      )
    } else if (view.step) {
      body = (
        <Pill label={label}>
          <PillHead title={title} source={source} closeLabel={closeLabel} />
          <ActiveStep view={view} rect={rect} />
          {error}
        </Pill>
      )
    }
  }

  if (!body) return null
  return (
    <>
      {tracking && rect && createPortal(<GuideArrow rect={rect} reduceMotion={reduceMotion} />, document.body)}
      {body}
    </>
  )
}
