/**
 * The crew editor's overview pane: three facts, then the wiring diagram.
 *
 * The strip answers what the previous layout answered only by making the reader
 * assemble it from three separate explanation boxes — how often this crew runs
 * unattended, whether its storage is private, and which model it actually ends up
 * on. Those boxes are gone; the facts they carried are here, and each is a value
 * rather than a sentence.
 */
import { Boxes, Clock, Cpu, Database, FolderOpen, Users, Waypoints, Webhook } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import CrewOverviewDiagram, { type CrewWireNode } from './CrewOverviewDiagram'

export interface CrewOverviewPaneProps {
  hub: React.ReactNode
  /** Provider-branded name for the template field, e.g. "Agent Template". */
  templateLabel: string
  template: string
  workspace: string
  memoryStore: string
  /** The label to show for the model: either a pinned id or the inherit wording. */
  modelLabel: string
  /** True when nothing is pinned on the crew, so the value is an absence. */
  modelInherited: boolean
  /** What the pin resolves to once inheritance is applied. '' when unknown. */
  resolvedModel: string
  activeSchedules: number
  /** Schedules could not be read, so the count is unknown rather than zero. */
  schedulesUnknown?: boolean
  routingWords: number
  /** How many other crews point at this crew's workspace or memory store. Drives
   *  the stat only, where the OR is the honest reading of "its storage". */
  sharingCrews: number
  /** Another crew points at this crew's workspace. Separate from the memory flag:
   *  tagging both from one OR-ed value labels a private workspace "Shared"
   *  whenever only the memory store is. */
  workspaceShared: boolean
  /** Another crew points at this crew's memory store. */
  memoryShared: boolean
  /** Webhook tokens bound to this crew. Unbound tokens are the pane's business,
   *  not the diagram's: charging them here would draw N solid inputs per crew
   *  for bindings that do not exist. */
  webhookTokens: number
  /** The webhook store could not be read — unknown rather than zero. */
  webhooksUnknown?: boolean
}

function Stat({ icon, value, label }: { icon?: React.ReactNode; value: string; label: string }) {
  return (
    // `basis` plus a min width so three cells WRAP on a narrow pane instead of
    // squeezing until their labels collide. `last:border-r-0` is not enough once
    // they wrap — a wrapped row's last cell is mid-strip — so the border is on
    // the leading edge of every cell but the first instead.
    <div className="min-w-[132px] flex-1 basis-full border-t border-border px-3 py-2 first:border-t-0
                    sm:basis-[132px] sm:border-l sm:border-t-0 sm:first:border-l-0">
      <div className="flex items-center gap-1.5 text-[15px] leading-tight text-text-strong">
        {icon}
        <span className="truncate">{value}</span>
      </div>
      <div className="mt-0.5 text-[10px] uppercase tracking-[0.06em] text-muted">{label}</div>
    </div>
  )
}

export default function CrewOverviewPane({
  hub, templateLabel, template, workspace, memoryStore, modelLabel, modelInherited,
  resolvedModel, activeSchedules, schedulesUnknown, routingWords, sharingCrews,
  workspaceShared, memoryShared, webhookTokens, webhooksUnknown,
}: CrewOverviewPaneProps) {
  const { t } = useTranslation()
  const unknown = t('components.crewEditor.stat_unknown')

  const inputs: CrewWireNode[] = [
    {
      key: 'schedules',
      icon: Clock,
      label: t('components.crewEditor.pane_schedules'),
      // A bare count, because the node's own label already names what is being
      // counted. Sidesteps a pluralised string whose grammar differs per
      // language for a number the label makes unambiguous.
      value: schedulesUnknown
        ? unknown
        : activeSchedules > 0 ? String(activeSchedules) : t('components.crewEditor.node_none'),
      muted: schedulesUnknown || activeSchedules === 0,
    },
    {
      key: 'routing',
      icon: Waypoints,
      label: t('pages.kiroCrewAgentsPage.triggers'),
      value: routingWords > 0 ? String(routingWords) : t('components.crewEditor.node_none'),
      muted: routingWords === 0,
    },
    {
      key: 'webhook',
      icon: Webhook,
      label: t('components.crewEditor.pane_webhook'),
      // Dashed only while NOTHING is bound: the ghost treatment means "a real
      // input that carries no crew binding", and once a token names this crew
      // that claim is false. The node reports that bindings EXIST; whether each
      // can currently call in is the rail badge's live/total and the pane's
      // per-row marker, because a disabled binding is still a binding. Unknown
      // keeps the ghost — a store that cannot be read is not evidence a
      // binding exists.
      value: webhooksUnknown
        ? unknown
        : webhookTokens > 0 ? String(webhookTokens) : t('components.crewEditor.webhook_unbound_short'),
      muted: webhooksUnknown || webhookTokens === 0,
      ghost: webhooksUnknown || webhookTokens === 0,
    },
  ]

  const outputs: CrewWireNode[] = [
    {
      key: 'template',
      icon: Boxes,
      label: templateLabel,
      value: template,
      mono: true,
    },
    {
      key: 'workspace',
      icon: FolderOpen,
      label: t('pages.kiroCrewAgentsPage.workspace_2'),
      value: workspace,
      mono: true,
      ...(workspaceShared ? { tag: t('components.crewEditor.tag_shared') } : {}),
    },
    {
      key: 'memory',
      icon: Database,
      label: t('pages.kiroCrewAgentsPage.memory_store'),
      value: memoryStore,
      mono: true,
      ...(memoryShared ? { tag: t('components.crewEditor.tag_shared') } : {}),
    },
    {
      key: 'model',
      icon: Cpu,
      label: t('pages.kiroCrewAgentsPage.model'),
      value: modelLabel,
      mono: !modelInherited,
      muted: modelInherited,
    },
  ]

  return (
    <div className="flex flex-col gap-3.5">
      <div className="flex flex-wrap rounded-lg border border-border bg-bg-accent">
        <Stat
          icon={<Clock className="lucide-inline h-[15px] w-[15px]" aria-hidden="true" />}
          value={schedulesUnknown ? unknown : String(activeSchedules)}
          label={t('components.crewEditor.stat_active_schedules')}
        />
        <Stat
          icon={<Users className="lucide-inline h-[15px] w-[15px]" aria-hidden="true" />}
          value={String(sharingCrews)}
          label={t('components.crewEditor.stat_crews_sharing')}
        />
        <Stat
          value={resolvedModel || unknown}
          label={t('components.crewEditor.stat_resolved_model')}
        />
      </div>
      <CrewOverviewDiagram
        inputs={inputs}
        outputs={outputs}
        inputsLabel={t('components.crewEditor.group_how_work_arrives')}
        outputsLabel={t('components.crewEditor.wire_what_it_works_with')}
        hub={hub}
      />
    </div>
  )
}
