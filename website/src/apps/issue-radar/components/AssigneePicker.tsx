import { useMemo, useState } from 'react'
import { Check, ChevronDown, Search, UserCheck } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
// `compareText` collates in the APP's language; a bare `localeCompare` reads the
// host locale and so orders the roster by the browser's language rather than the
// one the dashboard is set to.
import { compareText } from '../../../i18n/format'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
} from '../../../components/ui/dropdown-menu'

/** The assignee editor: ONE dropdown control that opens the repo roster.
 *
 * A dropdown rather than a grid of member chips, for two reasons that point the
 * same way. `max-two-buttons-per-row` caps a horizontal action group at two
 * controls, and a roster renders N sibling buttons -- the rule's own prescribed fix
 * is an overflow menu, which counts as one. It is also what the forges themselves
 * do for this exact job, so the affordance is the one a triager already expects.
 *
 * Selection is matched CASE-INSENSITIVELY: the roster and the issue's assignee list
 * can spell the same person with different case, and a case-sensitive compare would
 * render an already-assigned member as unassigned and then try to add a duplicate.
 *
 * An assignee the roster does NOT list still renders, in its own group, and is still
 * removable -- someone assigned upstream, or a former member, must not be stranded
 * on the issue with no way to take them off. That is also why an empty roster does
 * not short-circuit while such assignees exist.
 */
export default function AssigneePicker({
  members, selected, onToggle, me, atCap, emptyText,
}: {
  members: string[]
  selected: string[]
  onToggle: (login: string) => void
  me?: string | null
  atCap?: boolean
  emptyText?: string
}) {
  const [q, setQ] = useState('')
  const sel = useMemo(() => new Set(selected.map((s) => s.toLowerCase())), [selected])

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const arr = needle ? members.filter((m) => m.toLowerCase().includes(needle)) : members
    // Selected first, then "me" ahead of the rest, then collated — so the two
    // things a triager acts on are never buried in a long roster.
    return [...arr].sort((a, b) => {
      const sa = sel.has(a.toLowerCase()) ? 0 : 1
      const sb = sel.has(b.toLowerCase()) ? 0 : 1
      if (sa !== sb) return sa - sb
      const ma = me && a.toLowerCase() === me.toLowerCase() ? 0 : 1
      const mb = me && b.toLowerCase() === me.toLowerCase() ? 0 : 1
      if (ma !== mb) return ma - mb
      return compareText(a, b)
    })
  }, [members, q, sel, me])

  // Assignees on the issue that the roster does not list — still removable.
  const orphans = useMemo(
    () => selected.filter((n) => !members.some((m) => m.toLowerCase() === n.toLowerCase())),
    [selected, members],
  )

  // Only truly nothing to show: no roster AND nobody assigned. With assignees
  // present the menu still has to open, or their removal controls are unreachable.
  if (members.length === 0 && orphans.length === 0) {
    return (
      <div className="text-[12px] text-muted py-2">
        {emptyText ?? i18nT('apps.issueRadar.components.assigneePicker.no_members')}
      </div>
    )
  }

  const row = (login: string, isOrphan: boolean) => {
    const isSel = sel.has(login.toLowerCase())
    const isMe = !!me && login.toLowerCase() === me.toLowerCase()
    // Adding is blocked at the cap; a selected member can always be removed, or
    // the editor would be a dead end.
    const disabled = !isSel && !!atCap
    return (
      <button
        key={login}
        type="button"
        role="menuitemcheckbox"
        aria-checked={isSel}
        onClick={() => { if (!disabled) onToggle(login) }}
        disabled={disabled}
        title={isOrphan
          ? i18nT('apps.issueRadar.components.assigneePicker.not_a_member', { login })
          : (isMe ? i18nT('apps.issueRadar.components.assigneePicker.this_is_you', { login }) : login)}
        className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] text-left ${
          disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer hover:bg-bg-elevated'
        }`}
      >
        <span className="w-4 flex-shrink-0 text-accent">
          {isSel ? <Check size={14} /> : null}
        </span>
        <span className="truncate flex-1">{login}</span>
        {isMe && <UserCheck size={12} className="flex-shrink-0 text-accent" />}
      </button>
    )
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="w-full flex items-center justify-between gap-2 px-2.5 py-1.5 text-[13px] rounded-md border border-border bg-bg text-text hover:border-accent cursor-pointer"
        >
          <span className="truncate">
            {selected.length > 0
              ? selected.join(', ')
              : i18nT('apps.issueRadar.components.assigneePicker.choose_assignees')}
          </span>
          <ChevronDown size={13} className="flex-shrink-0 text-muted" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[240px] p-1.5">
        <div className="relative mb-1.5">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted pointer-events-none" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            // Radix moves focus to the first item on open and treats typing as
            // type-ahead; keeping both off the field is what lets the filter be
            // typed into at all.
            onKeyDown={(e) => e.stopPropagation()}
            placeholder={i18nT('apps.issueRadar.components.assigneePicker.filter_members')}
            aria-label={i18nT('apps.issueRadar.components.assigneePicker.filter_members')}
            className="w-full pl-8 pr-2 py-1.5 text-[13px] rounded-md border border-border bg-bg text-text placeholder:text-muted outline-none focus:border-accent"
          />
        </div>

        <div className="max-h-[240px] overflow-y-auto">
          {filtered.map((login) => row(login, false))}

          {orphans.length > 0 && (
            <>
              <div className="text-[11px] text-muted px-2 pt-2 pb-1">
                {i18nT('apps.issueRadar.components.assigneePicker.assigned_but_not_a_member')}
              </div>
              {orphans.map((login) => row(login, true))}
            </>
          )}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
