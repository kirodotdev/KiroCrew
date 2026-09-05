import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { ArrowRightLeft, AlertTriangle } from 'lucide-react'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'

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
  const { data: instanceData } = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    staleTime: 30_000,
    retry: false,
  })
  const crews = instanceData?.instances ?? []

  const submit = async () => {
    const target = toCrew.trim()
    if (!target) {
      setError(i18nT('components.moveToCrew.error_target_required'))
      return
    }
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
    <div className="move-to-crew" role="dialog" aria-label={i18nT('components.moveToCrew.dialog_label', { unitKind })}>
      <h3>
        <ArrowRightLeft size={16} aria-hidden="true" />{' '}
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
          components={{ id: <code /> }}
        />
      </h3>

      {/* `aria-labelledby` rather than relying on the htmlFor/id pair alone:
          jsx-a11y/control-has-associated-label only reads a control's own text
          content or its aria-* naming, not a sibling <label>'s htmlFor, so the
          pair alone trips the lint gate. Pointing at the same visible label
          keeps ONE source of truth for the accessible name. */}
      <label id="move-to-crew-target-label" htmlFor="move-to-crew-target">{i18nT('components.moveToCrew.target_crew_label')}</label>
      <input
        id="move-to-crew-target"
        aria-labelledby="move-to-crew-target-label"
        value={toCrew}
        onChange={e => setToCrew(e.target.value)}
        placeholder={i18nT('components.moveToCrew.target_crew_placeholder')}
        autoComplete="off"
        list={crews.length > 0 ? 'move-to-crew-crews' : undefined}
      />
      {/* Degrades to a plain text field when nothing is configured: an empty
          picker would be a dead end, and free text still reaches the endpoint. */}
      {crews.length > 0 && (
        <datalist id="move-to-crew-crews">
          {crews.map(c => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </datalist>
      )}

      <div className="move-to-crew-actions">
        <button type="button" onClick={submit} disabled={busy}>
          {busy ? i18nT('components.moveToCrew.planning') : i18nT('components.moveToCrew.plan_move')}
        </button>
        <button type="button" onClick={onClose}>{i18nT('components.moveToCrew.cancel')}</button>
      </div>

      {error && <p className="move-to-crew-error" role="alert">{error}</p>}

      {plan && (
        <div className="move-to-crew-plan">
          <dl>
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
                    <span className="sev">({r.severity})</span>
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

          <p className="move-to-crew-note">
            {i18nT('components.moveToCrew.plan_only_note_prefix')} <strong>{i18nT('components.moveToCrew.plan_only_note_emphasis')}</strong> {i18nT('components.moveToCrew.plan_only_note_suffix')}
          </p>
        </div>
      )}
    </div>
  )
}
