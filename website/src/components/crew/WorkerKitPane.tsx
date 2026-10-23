/**
 * WorkerKitPane — the crewmate editor's Advanced pane: the worker's extra kit.
 *
 * A crewmate hands its work to a worker session. The owner can name ONE agent
 * template whose instructions, connections, skills and auto-approvals are
 * copied into the crewmate's own config once; after that the copy does not
 * follow the template. Copying (or re-copying) always shows what would change
 * first and writes only when the owner confirms, and when the config holds
 * things added since the last copy it asks whether to keep them.
 */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Minus, PenLine, Plus, Power, PowerOff, type LucideIcon } from 'lucide-react'
import { Btn, Checkbox, PanelSectionHeader } from '../ui'
import SimpleSelect from '../SimpleSelect'
import ErrorNotice from '../ErrorNotice'
import { i18nT } from '../../i18n/t'
import { kitTemplateName, templateDisplayName } from './builtFromName'
import { changeLabel } from './workerKitLabels'
import { workerKitErrorText } from './workerKitErrors'
import { groupWorkerKitChanges } from './workerKitGroups'
import { apiErrorCode } from '../../api/apiError'
import {
  crewCapabilitiesKey,
  crewWorkerKitApi,
  crewWorkerKitKey,
  type WorkerKitChange,
  type WorkerKitPreview,
} from '../../api/crewCapabilities'

// Each change's word as a badge tinted by its hue, with a hue-coloured glyph.
// The word is painted in `text-text`, not the hue: a plain status hue on its
// own tint falls under 4.5:1 in some dark palettes (kiro-dark `--ok` reads
// 3.08:1), while the glyph only needs the 3:1 non-text floor. The word stays,
// so the colour is never the only cue.
const CHANGE_BADGES: Record<WorkerKitChange['change'], { label: () => string; tint: string; Icon: LucideIcon; ink: string }> = {
  added: { label: () => i18nT('crewWorkerKit.change_added'), tint: 'bg-ok-subtle', Icon: Plus, ink: 'text-ok-text' },
  changed: { label: () => i18nT('crewWorkerKit.change_changed'), tint: 'bg-warn-subtle', Icon: PenLine, ink: 'text-warn-text' },
  removed: { label: () => i18nT('crewWorkerKit.change_removed'), tint: 'bg-danger-subtle', Icon: Minus, ink: 'text-danger-text' },
}

// Under "Turned off" the row IS a restriction, so its badge states the effect:
// adding one turns a tool off, removing one turns it back on.
const RESTRICTION_BADGES: Record<WorkerKitChange['change'], typeof CHANGE_BADGES[WorkerKitChange['change']]> = {
  added: { label: () => i18nT('crewWorkerKit.change_turns_off'), tint: 'bg-danger-subtle', Icon: PowerOff, ink: 'text-danger-text' },
  changed: CHANGE_BADGES.changed,
  removed: { label: () => i18nT('crewWorkerKit.change_turns_on'), tint: 'bg-ok-subtle', Icon: Power, ink: 'text-ok-text' },
}

function ChangeBadge({ change, restriction }: { change: WorkerKitChange['change']; restriction: boolean }) {
  const { label, tint, Icon, ink } = (restriction ? RESTRICTION_BADGES : CHANGE_BADGES)[change]
  return (
    <span
      className={`inline-flex min-w-[5rem] shrink-0 items-center gap-1 rounded-full px-2 py-[2px] text-[11px] font-medium text-text ${tint}`}
      data-testid="worker-kit-change-badge"
      data-change={change}
    >
      <Icon size={12} className={`lucide-inline shrink-0 ${ink}`} aria-hidden="true" />
      {label()}
    </span>
  )
}

export default function WorkerKitPane({
  member,
  name,
  templates,
}: {
  /** The crewmate's key, for the API only — never rendered. */
  member: string
  /** The crewmate's display name. */
  name: string
  /** Installed agent templates that can be copied in. */
  templates: string[]
}) {
  const queryClient = useQueryClient()
  const kitQuery = useQuery({
    queryKey: crewWorkerKitKey(member),
    queryFn: () => crewWorkerKitApi.get(member),
    retry: false,
  })
  const kit = kitQuery.data
  const [picked, setPicked] = useState('')
  const [keep, setKeep] = useState(true)
  const [preview, setPreview] = useState<WorkerKitPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<unknown>(null)

  // The template the kit came from, preselected: for a private copy, the
  // template that copy was forked from. Picking another one switches the kit.
  const current = kit?.private_copy ? kit.origin || '' : kit?.template || ''
  const template = picked || (templates.includes(current) ? current : '')
  const isRecopy = !!current && template === current
  const askKeep = !!kit?.has_additions_since
  // The preview button's label, which a refusal names as the way forward.
  const previewLabel = isRecopy ? i18nT('crewWorkerKit.recopy') : i18nT('crewWorkerKit.copy')

  const reset = () => { setPreview(null); setErr(null) }

  const runPreview = async () => {
    if (!template) return
    setBusy(true)
    setErr(null)
    try {
      setPreview(await crewWorkerKitApi.preview(member, { template, keep_additions: askKeep ? keep : true }))
    } catch (e) {
      setErr(e)
    } finally {
      setBusy(false)
    }
  }

  const apply = async () => {
    if (!preview) return
    setBusy(true)
    setErr(null)
    try {
      await crewWorkerKitApi.apply(member, {
        template: preview.template,
        keep_additions: askKeep ? keep : true,
        revision: preview.revision,
      })
      setPreview(null)
      setPicked('')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: crewWorkerKitKey(member) }),
        queryClient.invalidateQueries({ queryKey: crewCapabilitiesKey(member) }),
      ])
    } catch (e) {
      // The preview no longer matches the file or the template: it cannot be
      // applied, so it goes, and the message asks for a fresh one.
      if (apiErrorCode(e) === 'stale_revision') setPreview(null)
      setErr(e)
    } finally {
      setBusy(false)
    }
  }

  if (kitQuery.isLoading) {
    return (
      <div className="flex items-center gap-2 text-[12px] text-muted">
        <Loader2 size={14} className="lucide-inline animate-spin" />
        {i18nT('crewWorkerKit.loading')}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-4" style={{ maxWidth: 560 }} data-testid="next-pane-advanced">
      <div className="flex flex-col gap-1">
        <span className="text-[12.5px] font-medium text-text">{i18nT('crewWorkerKit.title')}</span>
        <p className="m-0 text-[12px] leading-relaxed text-muted">{i18nT('crewWorkerKit.body', { name })}</p>
      </div>

      {/* No hand-off: the pane holds an unconfirmed preview; the hand-off would
          navigate away from the open editor and lose it. */}
      <ErrorNotice
        message={workerKitErrorText(kitQuery.error) ?? workerKitErrorText(err, previewLabel)}
        variant="inline"
        askAgent={false}
        testId="worker-kit-error"
      />

      {kit && (
        <>
          <span className="text-[12px] text-muted" data-testid="worker-kit-current">
            {!kit.template
              ? i18nT('crewWorkerKit.none_copied')
              : kit.private_copy && !kit.origin
                ? i18nT('crewWorkerKit.copied_from_own_copy')
                : i18nT('crewWorkerKit.copied_from', { template: kitTemplateName(kit) })}
          </span>

          <div className="flex flex-col gap-1.5">
            <span className="text-[12px] text-text">{i18nT('crewWorkerKit.template_label')}</span>
            <SimpleSelect
              options={templates}
              optionLabels={templates.map(templateDisplayName)}
              value={template}
              onChange={v => { setPicked(v); reset() }}
              disabled={busy}
              triggerFallback={i18nT('crewWorkerKit.pick_template')}
              aria-label={i18nT('crewWorkerKit.template_label')}
            />
          </div>

          {askKeep && (
            <label className="flex items-center gap-2 text-[12px] text-text">
              <Checkbox
                checked={keep}
                onChange={e => { setKeep(e.target.checked); reset() }}
                disabled={busy}
                data-testid="worker-kit-keep"
              />
              {i18nT('crewWorkerKit.keep_additions')}
            </label>
          )}

          {!preview && (
            <div>
              <Btn onClick={runPreview} disabled={!template || busy} data-testid="worker-kit-preview">
                {previewLabel}
              </Btn>
            </div>
          )}

          {preview && (
            <div className="flex flex-col gap-3 rounded-md border border-border bg-bg-elevated p-3" data-testid="worker-kit-changes">
              <span className="text-[12.5px] font-medium text-text">
                {i18nT('crewWorkerKit.preview_title', {
                  template: (preview.template === kit.template ? kitTemplateName(kit) : null) ?? templateDisplayName(preview.template),
                })}
              </span>
              {preview.changes.length === 0 ? (
                <span className="text-[12px] text-muted">{i18nT('crewWorkerKit.no_changes')}</span>
              ) : (
                groupWorkerKitChanges(preview.changes).map(group => (
                  <section key={group.key} className="flex flex-col gap-1.5" data-testid={`worker-kit-group-${group.key}`}>
                    <PanelSectionHeader label={group.heading} count={group.changes.length} />
                    <ul className="m-0 flex list-none flex-col gap-1 p-0">
                      {group.changes.map(c => (
                        <li key={`${c.section}:${c.id}`} className="flex items-center gap-2 text-[12px]">
                          <ChangeBadge change={c.change} restriction={group.key === 'off'} />
                          {changeLabel(c) !== null && (
                            <span className="min-w-0 truncate text-text" title={c.id} data-testid="worker-kit-change-name">
                              {changeLabel(c)}
                            </span>
                          )}
                        </li>
                      ))}
                    </ul>
                  </section>
                ))
              )}
              <div className="flex items-center gap-2">
                <Btn onClick={reset} disabled={busy} data-testid="worker-kit-cancel">
                  {i18nT('crewWorkerKit.cancel')}
                </Btn>
                <Btn primary onClick={apply} disabled={busy} data-testid="worker-kit-apply">
                  {i18nT('crewWorkerKit.apply')}
                </Btn>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
