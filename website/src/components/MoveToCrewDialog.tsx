import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { ArrowRightLeft, AlertTriangle } from 'lucide-react'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './ui/dialog'

/**
 * Crew-to-crew work migration (issue #7577) — the shared "move to crew" dialog.
 *
 * One dialog for all three unit kinds (cron job, chat session, task run). It
 * asks for a target crew, calls the caller's plan function, and renders what the
 * plan says WOULD happen: the handoff id, how many allow-listed fields would
 * travel, the requirements the target must satisfy, and any advisory findings.
 *
 * It deliberately does NOT claim a move happened. The transmit / quiesce /
 * tombstone steps run over the crew tunnel and land with that wiring; showing a
 * plan is the honest thing to show until then, and the copy says so.
 */

export interface MovePlanRequirement {
  kind: string
  identity: string
  severity: string
}

export interface MovePlanFinding {
  kind: string
  detail: string
  severity: string
  detail_key: string
}

export interface MovePlan {
  handoff_id: string
  bundle_kind: string
  bundle_version: number
  target_crew: string
  ships: number
  requirements: MovePlanRequirement[]
  findings: MovePlanFinding[]
  completed_kept?: number
}

interface Props {
  unitKind: 'cron' | 'session' | 'taskrun'
  unitId: string
  onPlan: (toCrew: string) => Promise<{ ok?: boolean; plan?: MovePlan; error?: string }>
  onClose: () => void
}

export default function MoveToCrewDialog({ unitKind, unitId, onPlan, onClose }: Props) {
  const [toCrew, setToCrew] = useState('')
  const [plan, setPlan] = useState<MovePlan | null>(null)
  // Two distinct states, deliberately not one. A blank target is the user's own
  // in-progress input, so it belongs beside the field as a hint; ErrorNotice is
  // for something that FAILED and may deserve a retry. Sharing one state made a
  // blank field render as a failure and, worse, left no channel for the peer
  // lookup's own error, which then disappeared entirely.
  const [validationHint, setValidationHint] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // Configured peers, offered as SUGGESTIONS rather than as the only choices.
  // `to_crew` is free-form on the wire (the handler only rejects blank, and no
  // layer resolves it against the registry), so a closed <select> would remove
  // the ability to name a crew that is not a configured instance yet — and
  // would be an empty dead end on a single-crew install. Same shared queryKey
  // as SendToInstanceSubmenu, so this costs no extra request; `retry: false`
  // because listInstances throws 403 when the Instances feature is off, which
  // is a legitimate steady state, not a transient error.
  const { data: instanceData, error: instancesError } = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    staleTime: 30_000,
    retry: false,
  })
  const crews = instanceData?.instances ?? []

  // A 403 means the Instances feature is off — a legitimate steady state, and the
  // suggestions are optional, so it stays silent. Anything else is a real failure
  // and used to vanish: the list simply came back empty, which reads as "this
  // install has no peers" rather than "we could not ask". The status is checked
  // before the message because that is where the transport puts it; the message
  // is only a fallback for an error that carries the code in its text.
  const instancesForbidden =
    (instancesError as { status?: number } | null)?.status === 403 ||
    /\b403\b/.test(String((instancesError as Error | null)?.message ?? ''))
  const suggestionsError =
    instancesError && !instancesForbidden
      ? i18nT('components.moveToCrew.error_suggestions_unavailable')
      : ''

  const submit = async () => {
    const target = toCrew.trim()
    if (!target) {
      setValidationHint(i18nT('components.moveToCrew.error_target_required'))
      return
    }
    setValidationHint('')
    setBusy(true)
    setError('')
    setPlan(null)
    try {
      const res = await onPlan(target)
      if (res?.plan) setPlan(res.plan)
      else setError(res?.error || i18nT('components.moveToCrew.error_no_plan'))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={open => { if (!open) onClose() }}>
      <DialogContent maxWidth={640} hideClose>
        <DialogHeader>
          <ArrowRightLeft size={16} className="shrink-0 text-accent" aria-hidden="true" />
          <DialogTitle>
            {/* ONE key for the whole sentence, via `<Trans>`, because the unit id in
                the middle is markup. Splitting it into a prefix and a
                "from this crew to another crew" suffix handed translators a fragment
                they cannot reorder relative to the id -- and in languages that put
                the source phrase first, the sentence is impossible to build from the
                pieces. `check-source-strings.mjs` flags exactly this as
                leading-connector. */}
            <Trans
              i18nKey="components.moveToCrew.heading"
              values={{ unitKind, unitId }}
              components={[<code key="id" />]}
            />
          </DialogTitle>
        </DialogHeader>
        <DialogBody className="space-y-4">

      {/* `aria-labelledby` rather than relying on the htmlFor/id pair alone:
          jsx-a11y/control-has-associated-label only reads a control's own text
          content or its aria-* naming, not a sibling <label>'s htmlFor, so the
          pair alone trips the lint gate. Pointing at the same visible label
          keeps ONE source of truth for the accessible name. */}
        <div className="space-y-1.5">
          <label
            id="move-to-crew-target-label"
            htmlFor="move-to-crew-target"
            className="block text-[12px] font-medium text-text"
          >
            {i18nT('components.moveToCrew.target_crew_label')}
          </label>
          <input
            id="move-to-crew-target"
            aria-labelledby="move-to-crew-target-label"
            value={toCrew}
            onChange={e => setToCrew(e.target.value)}
            placeholder={i18nT('components.moveToCrew.target_crew_placeholder')}
            autoComplete="off"
            list={crews.length > 0 ? 'move-to-crew-crews' : undefined}
            className="h-9 w-full rounded-md border border-border bg-bg px-3 text-[13px] text-text outline-none placeholder:text-muted focus:border-accent focus:ring-1 focus:ring-accent"
          />
        </div>
      {/* Degrades to a plain text field when nothing is configured: an empty
          picker would be a dead end, and free text still reaches the endpoint. */}
      {crews.length > 0 && (
        <datalist id="move-to-crew-crews">
          {crews.map(c => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </datalist>
      )}

        {validationHint && (
          <p className="text-[12px] text-muted" data-testid="move-to-crew-hint">
            {validationHint}
          </p>
        )}

        {/* No agent hand-off here: `toCrew` is an unsaved local draft. Leaving
            askAgent at its safe default avoids navigating away and destroying
            the exact input the user needs to correct or retry. */}
        <ErrorNotice
          message={error || suggestionsError}
          onDismiss={() => setError('')}
          testId="move-to-crew-error"
        />

      {plan && (
        <div className="space-y-4 rounded-lg border border-border bg-bg-elevated p-4 text-[13px]">
          {/* Narrow-first: one column, so a label plus an unbreakable handoff id
              cannot exceed a 320px viewport's ~216px body and clip. The two-column
              form starts at the first breakpoint where it fits. `break-all` on the
              value covers the id itself, which has no break opportunities. */}
          {/* The correction leads the panel rather than trailing it, and is not
              muted: the rows below read like a completed-transfer receipt, so a
              user who skims stops at the first line. Decision-critical copy must
              not be the smallest text on the surface. */}
          <p
            className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 font-medium text-warn"
            data-testid="move-to-crew-plan-only"
          >
            {i18nT('components.moveToCrew.plan_only_note_prefix')}{' '}
            <strong>{i18nT('components.moveToCrew.plan_only_note_emphasis')}</strong>{' '}
            {i18nT('components.moveToCrew.plan_only_note_suffix')}
          </p>

          <dl className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-[max-content_1fr] sm:gap-y-2 [&_dd]:break-all [&_dd]:mb-2 [&_dd]:sm:mb-0">
            <dt>{i18nT('components.moveToCrew.handoff_id')}</dt><dd><code>{plan.handoff_id}</code></dd>
            <dt>{i18nT('components.moveToCrew.bundle')}</dt><dd>{plan.bundle_kind} v{plan.bundle_version}</dd>
            <dt>{i18nT('components.moveToCrew.fields_shipped')}</dt><dd>{plan.ships}</dd>
            {typeof plan.completed_kept === 'number' && (
              <>
                <dt>{i18nT('components.moveToCrew.completed_kept')}</dt><dd>{plan.completed_kept}</dd>
              </>
            )}
          </dl>

          {plan.requirements.length > 0 && (
            <>
              <h4>{i18nT('components.moveToCrew.requirements_heading')}</h4>
              <ul>
                {plan.requirements.map(r => (
                  <li key={`${r.kind}:${r.identity}`}>
                    <strong>{r.kind}</strong>: <code>{r.identity}</code>{' '}
                    <span className="text-muted">({r.severity})</span>
                  </li>
                ))}
              </ul>
            </>
          )}

          {plan.findings.length > 0 && (
            <>
              <h4><AlertTriangle size={14} aria-hidden="true" /> {i18nT('components.moveToCrew.findings_heading')}</h4>
              <ul>
                {plan.findings.map(f => (
                  <li key={f.detail_key}>
                    <strong>{f.detail_key}</strong>: {f.detail}
                  </li>
                ))}
              </ul>
            </>
          )}

        </div>
      )}
        </DialogBody>
        <DialogFooter>
          <button
            type="button"
            onClick={onClose}
            className="h-8 rounded-md border border-border bg-transparent px-3 text-[13px] text-muted transition-colors hover:bg-bg-hover hover:text-text"
          >
            {plan ? i18nT('components.dialog.close') : i18nT('components.moveToCrew.cancel')}
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={busy}
            className="h-8 rounded-md border-0 bg-accent px-3 text-[13px] font-semibold text-accent-fg transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? i18nT('components.moveToCrew.planning') : i18nT('components.moveToCrew.plan_move')}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
