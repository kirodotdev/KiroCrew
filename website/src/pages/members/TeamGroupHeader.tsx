import { ChevronRight, Users } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { cn } from '../../lib/utils'
import { ROW_ACTIVE_CLS, ROW_BOX_CLS, ROW_IDLE_CLS } from '../../components/listShell'
import type { RosterGroup } from './teamGroups'

/**
 * One team's header row in the grouped roster: icon, name, crewmate count, a
 * chevron for the fold. Its crewmate rows follow it, indented.
 *
 * The row is the team's OPEN gesture (it selects the team and shows the team
 * view in the main pane); the chevron is the fold. They are two controls, so
 * the chevron is a sibling button placed over the row's right padding -- a
 * button inside a button is invalid HTML and breaks keyboard activation -- the
 * same layout the crewmate rows use for their star. Because a heading that
 * OPENS rather than folds is not what a reader expects of a group header, the
 * row says what its click does: an "Open team" label sits at its right edge
 * (hidden once the team is the open one), and the button's accessible name
 * is "Open team <name>". The "No team" group has no team behind it: it opens
 * nothing, it only folds, so its whole row is the fold, it carries no such
 * label, and it reads muted.
 */
export default function TeamGroupHeader({
  group,
  selected,
  collapsed,
  onOpen,
  onToggle,
}: {
  group: RosterGroup
  selected: boolean
  collapsed: boolean
  onOpen: () => void
  onToggle: () => void
}) {
  const { t } = useTranslation()
  const isNoTeam = group.team === null
  const name = group.team ? group.team.name : t('pages.membersPage.team_none')
  const count = t('pages.membersPage.team_crewmate_count', { count: group.members.length })
  const foldLabel = t(collapsed ? 'pages.membersPage.team_expand' : 'pages.membersPage.team_collapse', { name })
  const iconCls = selected ? 'text-accent' : isNoTeam ? 'text-muted opacity-60' : 'text-muted'
  const nameCls = selected ? 'text-text-strong' : isNoTeam ? 'text-muted opacity-70' : 'text-muted'
  const chevron = (
    <ChevronRight
      size={13}
      className={cn(
        'lucide-inline text-muted shrink-0 transition-transform duration-150 motion-reduce:transition-none',
        collapsed ? '' : 'rotate-90',
      )}
      aria-hidden="true"
    />
  )
  const body = (
    <>
      <Users size={13} className={cn('lucide-inline shrink-0', iconCls)} aria-hidden="true" />
      {/* Under width pressure the NAME wins: the rule gives way first, then the
          count truncates, and only then the name -- the group's identifier must
          not lose to its ancillary text, or two similarly named teams read as
          one. */}
      <span className={cn('text-[11.5px] font-semibold truncate min-w-0 [flex-shrink:1]', nameCls)}>{name}</span>
      <span className="text-[10.5px] text-muted font-mono tabular-nums truncate min-w-0 [flex-shrink:8]">{count}</span>
      <span className="flex-1 min-w-2 h-px bg-border [flex-shrink:100]" />
    </>
  )
  if (isNoTeam) {
    return (
      <li className="group/row relative mt-1" data-testid="team-group" data-team={group.id}>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={!collapsed}
          aria-label={foldLabel}
          className={cn('w-full flex items-center gap-2 text-left select-none transition-all', ROW_BOX_CLS, 'py-1.5', ROW_IDLE_CLS)}
          data-testid="team-group-header"
          data-team={group.id}
        >
          {body}
          {chevron}
        </button>
      </li>
    )
  }
  return (
    <li className="group/row relative mt-1 first:mt-0" data-testid="team-group" data-team={group.id}>
      <button
        type="button"
        onClick={onOpen}
        aria-current={selected ? 'true' : undefined}
        aria-label={t('pages.membersPage.team_open_named', { name })}
        className={cn(
          'w-full flex items-center gap-2 text-left select-none transition-all',
          ROW_BOX_CLS,
          'py-1.5',
          // AFTER the box classes: tailwind-merge keeps the LAST padding it sees,
          // so `pr-8` listed before ROW_BOX_CLS lost to its `pr-3` and the
          // absolute chevron sat on the label's tail ("Open te ›").
          'pr-8',
          selected ? ROW_ACTIVE_CLS : ROW_IDLE_CLS,
        )}
        data-testid="team-group-header"
        data-team={group.id}
      >
        {body}
        {!selected && (
          <span className="text-[10.5px] text-accent shrink-0 whitespace-nowrap" data-testid="team-group-open">
            {t('pages.membersPage.team_open')}
          </span>
        )}
      </button>
      {/* 24x24 target over the row's right padding, like the crewmate rows'
          star: a touch beside the glyph must fold the group, not open it. */}
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation()
          onToggle()
        }}
        aria-expanded={!collapsed}
        aria-label={foldLabel}
        title={foldLabel}
        className="absolute right-1 top-1/2 -translate-y-1/2 flex items-center justify-center w-6 h-6 rounded hover:bg-bg-hover"
        data-testid="team-group-toggle"
        data-team={group.id}
      >
        {chevron}
      </button>
    </li>
  )
}
