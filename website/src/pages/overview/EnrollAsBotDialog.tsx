/**
 * EnrollAsBotDialog — "Enroll as bot" on a custom agent.
 *
 * A bot owns its config file under its own name, so it can never be created
 * under the agent's name (`POST /api/agents` answers `template_name_in_use`),
 * and a create may only start from the built-in worker
 * (`mate_template_unsupported`). Enrolling is therefore the two steps the
 * product already has, done in one place: create a bot under a free name the
 * user can change, then add this agent's abilities to it through the same
 * reviewed add the bot editor's "Add abilities" pane makes.
 */
import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { apiErrorCode } from '../../api/apiError'
import { Btn } from '../../components/ui'
import Modal from '../../components/Modal'
import ErrorNotice from '../../components/ErrorNotice'
import WorkerKitPane from '../../components/crew/WorkerKitPane'
import { DEFAULT_CREWMATE_TEMPLATE } from '../members/NewCrewmateDialog'
import { defaultLookFor } from '../../lib/avatarPoses'
import { errMessage } from '../../utils/thunkError'
import { queryReadFailed } from '../../utils/queryReadFailed'
import { i18nT } from '../../i18n/t'

/** A bot name no listed name already holds: `<agent>-bot`, then `-2`, `-3`, … */
export function freeBotName(agent: string, taken: readonly string[]): string {
  const held = new Set(taken.map(n => n.toLowerCase()))
  const base = `${agent}-bot`
  if (!held.has(base.toLowerCase())) return base
  let n = 2
  while (held.has(`${base}-${n}`.toLowerCase())) n += 1
  return `${base}-${n}`
}

export default function EnrollAsBotDialog({
  agent,
  agentNames,
  onCreated,
  onEnrolled,
  onClose,
}: {
  /** The custom agent being enrolled; `null` keeps the dialog closed. */
  agent: string | null
  /** Every custom agent's name: a bot cannot take one of them. */
  agentNames: readonly string[]
  /** The bot now exists (its abilities are not added yet). */
  onCreated: () => void
  /** The agent's abilities were added to the bot named here. */
  onEnrolled: (bot: string) => void
  onClose: () => void
}) {
  const open = agent !== null
  const roster = useQuery({
    queryKey: ['kirocrew-agents'],
    queryFn: () => api.kirocrewAgents(),
    enabled: open,
  })
  const rosterRows: { name: string; display_name?: string }[] = roster.data?.agents ?? []
  const botNames = rosterRows.flatMap(a => [a.name, a.display_name ?? ''].filter(Boolean))
  const taken = [...agentNames, ...botNames]
  // The field shows the suggestion until the user types; a typed name is theirs.
  const [typed, setTyped] = useState<string | null>(null)
  const name = typed ?? (agent ? freeBotName(agent, taken) : '')
  // Two surfaces, never one: `hint` is this form's own check on the name (no
  // request was made), `error` is a create the server refused or never answered.
  const [hint, setHint] = useState('')
  const [error, setError] = useState('')
  // The created bot: its config key (for the API) and the name it shows.
  const [bot, setBot] = useState<{ key: string; name: string } | null>(null)
  // Step two's state: the add is being previewed or written, and it has landed.
  const [kitBusy, setKitBusy] = useState(false)
  const [applied, setApplied] = useState(false)

  const close = () => {
    setTyped(null); setHint(''); setError(''); setBot(null); setKitBusy(false); setApplied(false)
    onClose()
  }

  const create = useMutation({
    mutationFn: (n: string) =>
      api.createKirocrewAgent({ name: n, kiro_agent: DEFAULT_CREWMATE_TEMPLATE, memory_store: 'default', avatar: defaultLookFor(n) }) as Promise<{ error?: string; name?: string }>,
    onSuccess: (r, n) => {
      if (r?.error) { setError(r.error); return }
      setBot({ key: r?.name || n, name: n })
      onCreated()
    },
    onError: (e: unknown, n) => {
      const code = apiErrorCode(e)
      setError(
        code === 'agent_exists'
          ? i18nT('pages.membersPage.create_name_taken', { name: n })
          : code === 'template_name_in_use'
            ? i18nT('pages.membersPage.create_name_reserved')
            : code === 'invalid_member_name'
              ? i18nT('pages.membersPage.create_name_unusable')
              : errMessage(e) || i18nT('pages.membersPage.create_failed'),
      )
    },
  })

  const submit = () => {
    const n = name.trim()
    if (!n || create.isPending) return
    setHint(''); setError('')
    // Said here rather than by the server: a name a custom agent or a bot
    // already holds is refused there, and the field is where to change it.
    const holds = (names: readonly string[]) => names.some(held => held.toLowerCase() === n.toLowerCase())
    if (holds(agentNames)) { setHint(i18nT('pages.membersPage.create_name_reserved')); return }
    if (holds(botNames)) { setHint(i18nT('pages.membersPage.create_name_taken', { name: n })); return }
    create.mutate(n)
  }

  // While a write is in flight the dialog stays: closing would unmount the pane
  // that reports whether the add landed, and the user would never see a failure.
  const writing = create.isPending || kitBusy

  return (
    <Modal
      open={open}
      onClose={() => { if (!writing) close() }}
      title={i18nT('pages.overview.agentTemplatesTab.enroll_title', { name: agent ?? '' })}
      maxWidth={600}
      guardAccidentalDismiss
      dismissDisabled={writing}
      footer={bot
        // One primary action at a time: until the add has landed the primary
        // button is the pane's own "Add", and this one only leaves without it.
        ? applied
          ? <Btn primary disabled={kitBusy} onClick={close} data-testid="enroll-done">{i18nT('pages.overview.agentTemplatesTab.enroll_done')}</Btn>
          : <Btn disabled={kitBusy} onClick={close} data-testid="enroll-skip">{i18nT('pages.overview.agentTemplatesTab.enroll_skip')}</Btn>
        : <>
            <Btn disabled={create.isPending} onClick={close}>{i18nT('pages.overview.agentTemplatesTab.cancel')}</Btn>
            <Btn primary disabled={!name.trim() || create.isPending} onClick={submit} data-testid="enroll-create">{i18nT('pages.overview.agentTemplatesTab.enroll_create')}</Btn>
          </>}
    >
      {!bot && (
        <form onSubmit={e => { e.preventDefault(); submit() }}>
          <p className="text-[12.5px] text-muted mb-3">{i18nT('pages.overview.agentTemplatesTab.enroll_intro')}</p>
          <label className="block text-[12px] text-muted mb-1" htmlFor="enroll-bot-name">{i18nT('pages.overview.agentTemplatesTab.enroll_name')}</label>
          <input
            id="enroll-bot-name"
            aria-label={i18nT('pages.overview.agentTemplatesTab.enroll_name')}
            aria-invalid={hint ? true : undefined}
            aria-describedby={hint ? 'enroll-bot-name-hint' : undefined}
            className="w-full px-2.5 py-1.5 rounded-md border border-border bg-bg-elevated text-[12.5px] text-text"
            value={name}
            onChange={e => { setTyped(e.target.value); setHint(''); setError('') }}
            autoFocus
          />
          {hint && (
            <span id="enroll-bot-name-hint" role="alert" className="mt-1.5 block text-[11.5px] leading-relaxed text-danger" data-testid="enroll-name-hint">
              {hint}
            </span>
          )}
          {/* No hand-off on either notice: the name typed into this dialog is
              unsaved until the create succeeds. */}
          <ErrorNotice
            message={queryReadFailed(roster) ? i18nT('pages.overview.agentTemplatesTab.enroll_roster_failed') : ''}
            className="mt-3"
            testId="enroll-roster-error"
          />
          <ErrorNotice message={error} className="mt-3" testId="enroll-error" />
        </form>
      )}
      {bot && agent && (
        <div className="flex flex-col gap-3">
          <p className="m-0 text-[12.5px] text-muted" role="status" data-testid="enroll-created">
            {i18nT('pages.overview.agentTemplatesTab.enroll_created', { name: bot.name })}
          </p>
          <WorkerKitPane
            member={bot.key}
            name={bot.name}
            templates={[agent]}
            initialTemplate={agent}
            onBusyChange={setKitBusy}
            onApplied={() => { setApplied(true); onEnrolled(bot.name) }}
          />
        </div>
      )}
    </Modal>
  )
}
