/**
 * The crew editor's section registry — the single list the rail, its badges and
 * the overview diagram all render from.
 *
 * Why a registry rather than a hand-written rail: a crew accumulates surfaces
 * (skills, tools, permissions, an activity log) faster than a stacked-section
 * dialog can absorb them, and each one arriving as fresh JSX is how the previous
 * layout reached four prose sections for five controls. Adding a surface here is
 * one entry plus one pane; nothing about the shell is edited.
 *
 * The groups are phrased as questions about the crew rather than as
 * configuration nouns ("Runtime binding"). That is not decoration: a
 * config-shaped grouping has no slot for "what is it allowed to do" or "what has
 * it done", so those surfaces would have had to invent a group each, and the
 * rail's meaning would drift with every addition.
 *
 * Built inside a hook, never as a module constant: a module-level array of
 * translated labels is evaluated at import time and freezes whichever language
 * was active then, which is why `scripts/i18n-codemod.mjs` refuses to convert
 * one.
 */
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Boxes, Clock, Cpu, FolderOpen, LayoutDashboard, Trash2, Waypoints, Webhook,
  type LucideIcon,
} from 'lucide-react'

/** Which pane the editor body is showing. */
export type CrewPaneKey =
  | 'overview' | 'template' | 'model' | 'place' | 'schedules' | 'routing' | 'webhook' | 'danger'

export interface CrewEditorSection {
  key: CrewPaneKey
  /** Rail group heading. Consecutive rows sharing one are rendered under it. */
  group: string
  icon: LucideIcon
  label: string
  /** Trailing count, e.g. `2/3` schedules. Rendered verbatim, so pre-format it. */
  count?: string
  /** Another crew points at this row's workspace or memory store. Drawn as a dot,
   *  which carries no text, so it never stands alone as meaning. */
  shared?: boolean
  /**
   * Rendered but unselectable — a surface the product knows about and cannot
   * serve yet. Shown rather than hidden so the gap is visible instead of being
   * absent, and so enabling it later is deleting a flag.
   */
  disabled?: boolean
  /** Why it is unselectable. Required alongside `disabled` — a greyed row with
   *  no reason reads as a bug rather than as a known gap. */
  reason?: string
  /** Pinned to the rail's foot, below the groups, away from navigation. */
  foot?: boolean
  /**
   * This pane holds an edit that has not been saved.
   *
   * A stacked form showed every pending change at once; a rail does not, so
   * Cancel could discard an edit made three panes ago with nothing on screen
   * admitting it exists. Distinct from `tone`, which reports a configuration
   * fact — a row can be both shared AND edited.
   */
  dirty?: boolean
}

/** What the registry needs from the page to fill counts and dots. */
export interface CrewEditorFacts {
  /** Provider-branded name for the template field, e.g. "Agent Template". Taken
   *  from the provider rather than a catalog key of its own, so the rail and the
   *  field it navigates to cannot end up calling the same thing two names. */
  templateLabel: string
  /** Schedules that currently wake this crew and are not paused. */
  activeSchedules: number
  /** Every schedule bound to this crew, paused included. */
  totalSchedules: number
  /** How many routing keywords the crew offers the orchestrator. */
  routingWords: number
  /** Another crew points at this crew's workspace or memory store. */
  sharesStorage: boolean
  /** False for the default crew, which cannot be removed. */
  canDelete: boolean
  /** Schedules could not be loaded, so counts are unknown rather than zero. */
  schedulesUnknown?: boolean
  /** Webhook tokens bound to this crew (unbound tokens are not counted: they
   *  can wake any crew, so charging them to every row would read as N bindings
   *  that do not exist — the pane itself discloses them instead). */
  webhookTokens: number
  /** The subset of those whose per-source admission switch is on. A disabled
   *  binding exists but cannot call in, so counting it as live would overstate
   *  the wake surface the same way ignoring the global kill switch would. */
  webhookTokensActive: number
  /** The webhook store could not be loaded — unknown rather than zero. */
  webhooksUnknown?: boolean
  /** Panes whose controls differ from what is saved on the crew. */
  dirtyPanes: ReadonlySet<CrewPaneKey>
}

/**
 * Resolve the rail for one crew.
 *
 * `t` is a real dependency: it subscribes to the language, so a memo keyed only
 * on the facts would keep whichever language's labels it first computed.
 */
export function useCrewEditorSections(facts: CrewEditorFacts): CrewEditorSection[] {
  const { t } = useTranslation()
  const {
    templateLabel, activeSchedules, totalSchedules, routingWords, sharesStorage, canDelete,
    schedulesUnknown, webhookTokens, webhookTokensActive, webhooksUnknown, dirtyPanes,
  } = facts
  return useMemo(() => {
    const rows: CrewEditorSection[] = [
      {
        key: 'overview',
        group: t('components.crewEditor.group_who_it_is'),
        icon: LayoutDashboard,
        label: t('components.crewEditor.pane_overview'),
      },
      {
        key: 'template',
        group: t('components.crewEditor.group_what_it_can_do'),
        icon: Boxes,
        label: templateLabel,
      },
      {
        key: 'model',
        group: t('components.crewEditor.group_what_it_can_do'),
        icon: Cpu,
        label: t('pages.kiroCrewAgentsPage.model'),
      },
      {
        key: 'place',
        group: t('components.crewEditor.group_where_it_works'),
        icon: FolderOpen,
        label: t('components.crewEditor.pane_workspace_memory'),
        ...(sharesStorage ? { shared: true } : {}),
      },
      // Routing sits ABOVE schedules deliberately: the wake pane's own
      // disambiguator says "Separate from Triggers above", and that sentence is
      // only true while the row it names really is above it.
      {
        key: 'routing',
        group: t('components.crewEditor.group_how_work_arrives'),
        icon: Waypoints,
        label: t('pages.kiroCrewAgentsPage.triggers'),
        ...(routingWords > 0 ? { count: String(routingWords) } : {}),
      },
      {
        key: 'schedules',
        group: t('components.crewEditor.group_how_work_arrives'),
        icon: Clock,
        label: t('components.crewEditor.pane_schedules'),
        // An unreadable schedule list must not render as "0/0": absence of an
        // answer and an answer of none are different claims about the crew.
        ...(schedulesUnknown ? {} : { count: `${activeSchedules}/${totalSchedules}` }),
      },
      {
        key: 'webhook',
        group: t('components.crewEditor.group_how_work_arrives'),
        icon: Webhook,
        label: t('components.crewEditor.pane_webhook'),
        // Same counting language as the Schedules row above: live/total over
        // the BOUND tokens, so a disabled binding is visible as a shortfall
        // rather than counted as a live wake path. Only tokens bound to this
        // crew are counted; the pane itself discloses unbound ones. No badge
        // at zero — nothing is bound, so there is no ratio to report.
        ...(webhooksUnknown || webhookTokens === 0
          ? {}
          : { count: `${webhookTokensActive}/${webhookTokens}` }),
      },
    ]
    for (const row of rows) {
      if (dirtyPanes.has(row.key)) row.dirty = true
    }
    if (canDelete) {
      rows.push({
        key: 'danger',
        group: '',
        icon: Trash2,
        // The pane's NAME, not its verb: the destructive button inside it is
        // already called "Delete crew", and two controls sharing one
        // accessible name is ambiguous to a screen reader and to a test.
        label: t('pages.kiroCrewAgentsPage.danger_zone'),
        foot: true,
      })
    }
    return rows
    // Primitives, not the `facts` object: a caller building it inline gets a new
    // identity every render, which would make the memo never hit.
  }, [templateLabel, activeSchedules, totalSchedules, routingWords, sharesStorage, canDelete,
    schedulesUnknown, webhookTokens, webhookTokensActive, webhooksUnknown, dirtyPanes, t])
}
