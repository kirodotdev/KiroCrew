/**
 * PreferencesTab — full preference management with user-defined groups.
 *
 * Two decoupled layers:
 * - UI: user organizes with tags/groups (purely for their mental model)
 * - AI: retrieves via RAG (ignores group structure, uses embeddings)
 *
 * CRUD operations hit /api/apps/personal-shopper/* endpoints.
 */

import { useCallback, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Folder, FolderPlus, Pencil, Plus, Tag, Trash2, X } from 'lucide-react'
import * as shopApi from './api'
import { Btn, EmptyState, Input } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'

import { i18nT } from '../../i18n/t'
// ── Types ──

interface Preference {
  id: string
  text: string
  tags: string[]
  created_at: string
  updated_at: string
}

interface Group {
  id: string
  name: string
  icon: string
  sort_order: number
}

// ── API helpers ──

async function fetchPreferences(): Promise<{ preferences: Preference[] }> {
  return shopApi.get('/preferences')
}

async function fetchGroups(): Promise<{ groups: Group[] }> {
  return shopApi.get('/groups')
}

async function addPreference(text: string, tags: string[]): Promise<{ id: string }> {
  return shopApi.post('/preferences', { text, tags })
}

async function updatePreference(id: string, data: { text?: string; tags?: string[] }): Promise<void> {
  await shopApi.put(`/preferences/${id}`, data)
}

async function deletePreference(id: string): Promise<void> {
  await shopApi.del(`/preferences/${id}`)
}

async function addGroup(name: string, icon: string): Promise<{ id: string }> {
  return shopApi.post('/groups', { name, icon })
}

async function deleteGroup(id: string): Promise<void> {
  await shopApi.del(`/groups/${id}`)
}

// ── Component ──

export function PreferencesTab() {
  const queryClient = useQueryClient()
  const [newPrefText, setNewPrefText] = useState('')
  const [newPrefGroup, setNewPrefGroup] = useState('')
  const [newGroupName, setNewGroupName] = useState('')
  const [showGroupForm, setShowGroupForm] = useState(false)

  const { data: prefsData, isLoading: prefsLoading } = useQuery({
    queryKey: ['personal-shopper', 'preferences'],
    queryFn: fetchPreferences,
  })

  const { data: groupsData } = useQuery({
    queryKey: ['personal-shopper', 'groups'],
    queryFn: fetchGroups,
  })

  const invalidate = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['personal-shopper'] })
  }, [queryClient])

  const addPrefMutation = useMutation({
    mutationFn: ({ text, tags }: { text: string; tags: string[] }) => addPreference(text, tags),
    onSuccess: () => { invalidate(); setNewPrefText('') },
  })

  const deletePrefMutation = useMutation({
    mutationFn: (id: string) => deletePreference(id),
    onSuccess: invalidate,
  })

  const addGroupMutation = useMutation({
    mutationFn: ({ name, icon }: { name: string; icon: string }) => addGroup(name, icon),
    onSuccess: () => { invalidate(); setNewGroupName(''); setShowGroupForm(false) },
  })

  const deleteGroupMutation = useMutation({
    mutationFn: (id: string) => deleteGroup(id),
    onSuccess: invalidate,
  })

  const preferences: Preference[] = prefsData?.preferences ?? []
  const groups: Group[] = groupsData?.groups ?? []
  // Tag ids are opaque, so a row needs this to show a name rather than hex.
  const groupNames = new Map(groups.map((g) => [g.id, g.name]))

  if (prefsLoading) {
    return <div className="text-sm text-[var(--muted)] py-8 text-center">{i18nT('apps.personalShopper.preferencesTab.loading_preferences')}</div>
  }

  // Group preferences by tag
  const grouped = new Map<string, Preference[]>()
  const ungrouped: Preference[] = []

  for (const pref of preferences) {
    if (pref.tags.length === 0) {
      ungrouped.push(pref)
    } else {
      for (const tag of pref.tags) {
        if (!grouped.has(tag)) grouped.set(tag, [])
        grouped.get(tag)!.push(pref)
      }
    }
  }

  return (
    <div className="space-y-4">
      {/* Add preference input */}
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (newPrefText.trim()) {
            // A group IS a tag holding the group's id -- that is the only thing
            // the grouping pass below reads. Sending [] unconditionally, as this
            // did, made every user-created group permanently unfillable.
            addPrefMutation.mutate({
              text: newPrefText.trim(),
              tags: newPrefGroup ? [newPrefGroup] : [],
            })
          }
        }}
      >
        <Input
          value={newPrefText}
          onChange={(e) => setNewPrefText(e.target.value)}
          placeholder={i18nT('apps.personalShopper.preferencesTab.add_a_preference_e_g_shoe_size_us_10')}
          className="flex-1"
        />
        {groups.length > 0 && (
          <SimpleSelect
            options={groups.map((g) => g.id)}
            optionLabels={groups.map((g) => g.name)}
            value={newPrefGroup}
            onChange={setNewPrefGroup}
            clearLabel={i18nT('apps.personalShopper.preferencesTab.no_group')}
            aria-label={i18nT('apps.personalShopper.preferencesTab.assign_to_group')}
          />
        )}
        <Btn
          type="submit"
          disabled={!newPrefText.trim() || addPrefMutation.isPending}
        >
          <Plus size={14} /> {i18nT('apps.personalShopper.preferencesTab.add')}
        </Btn>
      </form>

      {/* Stated at the point of interaction, not only on the App Store card:
          the advisor cannot read this store, so a preference typed here does
          not reach it until the user says it in the conversation. Given the
          same weight as the form above it -- as a muted footnote it read as
          fine print next to an affordance that looks fully functional. */}
      <p
        role="note"
        className="text-sm px-3 py-2 rounded-lg bg-[var(--bg-elevated)] border border-[var(--border)] text-[var(--text)] leading-relaxed"
      >
        {i18nT('apps.personalShopper.preferencesTab.the_advisor_cannot_read_this_list_paste_what_ma')}
      </p>

      {preferences.length === 0 && (
        <EmptyState
          icon={<Tag size={28} />}
          title={i18nT('apps.personalShopper.preferencesTab.no_preferences_yet')}
          subtitle={i18nT('apps.personalShopper.preferencesTab.add_your_first_preference')}
        />
      )}

      {/* Groups */}
      {groups.map((group) => {
        const items = grouped.get(group.id) ?? []
        return (
          <section key={group.id} className="space-y-1">
            <div className="flex items-center justify-between">
              <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                {/* The lucide Folder is the group's icon. Rendering group.icon
                    here put arbitrary user-supplied text in an icon slot, which
                    bypasses the icon-library rule and duplicated the glyph
                    already to its left. The field is still accepted and stored
                    for a future picker; it is simply not a glyph today. */}
                <Folder size={12} />
                {group.name}
              </h3>
              <button
                onClick={() => deleteGroupMutation.mutate(group.id)}
                className="text-[var(--muted)] hover:text-[var(--danger)] transition-colors"
                title={i18nT('apps.personalShopper.preferencesTab.delete_group_keeps_preferences')}
                aria-label={i18nT('apps.personalShopper.preferencesTab.delete_named_group', { name: group.name })}
              >
                <X size={12} />
              </button>
            </div>
            {items.length === 0 ? (
              <p className="text-[11px] text-[var(--muted)] italic pl-2">{i18nT('apps.personalShopper.preferencesTab.no_items_in_this_group')}</p>
            ) : (
              <div className="space-y-1">
                {items.map((p) => (
                  <PreferenceRow
                    key={p.id}
                    pref={p}
                    onDelete={() => deletePrefMutation.mutate(p.id)}
                    onUpdate={invalidate}
                    groupNames={groupNames}
                    groups={groups}
                    sectionGroupId={group.id}
                  />
                ))}
              </div>
            )}
          </section>
        )
      })}

      {/* Ungrouped */}
      {ungrouped.length > 0 && (
        <section className="space-y-1">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
            {i18nT('apps.personalShopper.preferencesTab.ungrouped')}
          </h3>
          <div className="space-y-1">
            {ungrouped.map((p) => (
              <PreferenceRow
                key={p.id}
                pref={p}
                onDelete={() => deletePrefMutation.mutate(p.id)}
                onUpdate={invalidate}
                groupNames={groupNames}
                groups={groups}
              />
            ))}
          </div>
        </section>
      )}

      {/* Add group */}
      <div className="pt-2 border-t border-[var(--border)]">
        {showGroupForm ? (
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              if (newGroupName.trim()) {
                addGroupMutation.mutate({ name: newGroupName.trim(), icon: '' })
              }
            }}
          >
            <Input
              value={newGroupName}
              onChange={(e) => setNewGroupName(e.target.value)}
              placeholder={i18nT('apps.personalShopper.preferencesTab.group_name')}
              className="flex-1"
              autoFocus
            />
            <Btn type="submit" disabled={!newGroupName.trim()}>
              {i18nT('apps.personalShopper.preferencesTab.create')}
            </Btn>
            <Btn
              type="button"
             
             
              onClick={() => setShowGroupForm(false)}
            >
              {i18nT('apps.personalShopper.preferencesTab.cancel')}
            </Btn>
          </form>
        ) : (
          <button
            onClick={() => setShowGroupForm(true)}
            className="flex items-center gap-1 text-xs text-[var(--muted)] hover:text-[var(--accent)] transition-colors"
          >
            <FolderPlus size={14} /> {i18nT('apps.personalShopper.preferencesTab.add_group')}
          </button>
        )}
      </div>
    </div>
  )
}

// ── Row-scoped tag reassignment ──

/**
 * The tag a row's selector represents: the group section it is rendered under.
 *
 * A multi-group preference is rendered once per group it belongs to, so the
 * row's identity is its SECTION, not `tags[0]`. Seeding from `tags[0]` made the
 * editor under heading "B" open preselected to "A" and edit a membership the
 * user could not see.
 */
export function rowTagFor(prefTags: string[], sectionGroupId?: string): string {
  return sectionGroupId ?? prefTags[0] ?? ''
}

/**
 * Tags to PUT when a row's group selector moves from `openedWith` to `editGroup`.
 *
 * Rules, each one a defect this replaced:
 * - Replace `openedWith` IN PLACE and keep every other tag. The store treats the
 *   array as authoritative, so omitting a tag deletes that membership.
 * - An empty `editGroup` removes only this row's membership.
 * - Deduplicate: the user can pick a group the preference already belongs to,
 *   which would otherwise persist a duplicate tag and render duplicate-key rows.
 * - `openedWith === ''` means the row was ungrouped, so the pick is an addition.
 */
export function nextTagsForRowEdit(
  prefTags: string[],
  openedWith: string,
  editGroup: string,
): string[] {
  const next = openedWith
    ? prefTags.flatMap((t) =>
        t === openedWith ? (editGroup ? [editGroup] : []) : [t],
      )
    : editGroup
      ? [...prefTags, editGroup]
      : [...prefTags]
  return Array.from(new Set(next))
}

// ── Preference Row ──

function PreferenceRow({
  pref,
  onDelete,
  onUpdate,
  groupNames,
  groups,
  sectionGroupId,
}: {
  pref: Preference
  onDelete: () => void
  onUpdate: () => void
  /** Group id -> display name. Tag ids are opaque, so without this the row
   *  renders meaningless hex where the group's name belongs. */
  groupNames: Map<string, string>
  /** All groups — needed for the reassignment selector. */
  groups: Group[]
  /** The group whose section this row is rendered in, if any. Its own pill is
   *  pure restatement of the heading directly above it. */
  sectionGroupId?: string
}) {
  const [editing, setEditing] = useState(false)
  const [editText, setEditText] = useState(pref.text)
  const rowTag = rowTagFor(pref.tags, sectionGroupId)
  const [editGroup, setEditGroup] = useState(rowTag)
  // What the selector held when THIS form opened, used as the change baseline
  // rather than live `pref.tags` (which a refetch moves underneath an open form).
  const [openedWith, setOpenedWith] = useState(rowTag)

  // Reassignment is offered ONLY for a preference in at most one group.
  //
  // Two structural problems make a single-select unsafe for a multi-group
  // preference: it cannot represent more than one membership, and the PUT
  // replaces the whole tags array computed from a CACHED read — so two rows of
  // the same preference (it renders once per section) can each send an
  // authoritative array and overwrite the other's reassignment. Narrowing the
  // affordance removes the precondition instead of layering guards on it: a
  // preference with <= 1 tag renders in exactly one section, so a second
  // concurrent row for it cannot exist.
  //
  // This costs nothing today — the add form sends at most one tag and this
  // editor never grows the count, so a multi-group preference is only reachable
  // through the API. Such a preference shows its memberships read-only rather
  // than risking a silent lost update. Editing them needs an add/remove-tag
  // endpoint the server can merge, tracked as follow-up.
  const canReassign = pref.tags.length <= 1

  const startEditing = () => {
    setEditText(pref.text)
    setEditGroup(rowTag)
    setOpenedWith(rowTag)
    setEditing(true)
  }

  const updateMutation = useMutation({
    mutationFn: (data: { text?: string; tags?: string[] }) => updatePreference(pref.id, data),
    onSuccess: () => { setEditing(false); onUpdate() },
  })

  if (editing) {
    const nextTags = nextTagsForRowEdit(pref.tags, openedWith, editGroup)
    const textChanged = editText.trim() !== pref.text
    // `canReassign` also gates the payload, so a multi-group preference can
    // never ship a tags array even if a selector were rendered by mistake.
    const groupChanged = canReassign && editGroup !== openedWith
    const changed = textChanged || groupChanged
    return (
      <form
        className="flex items-center gap-2 px-3 py-2 rounded-lg bg-[var(--card)] border border-[var(--accent)]"
        onSubmit={(e) => {
          e.preventDefault()
          if (editText.trim() && changed) {
            // Omit `tags` unless the USER moved the selector in this form. The
            // backend treats a missing `tags` as "leave them alone", so a
            // text-only edit cannot disturb a grouping a sibling row changed.
            updateMutation.mutate(
              groupChanged
                ? { text: editText.trim(), tags: nextTags }
                : { text: editText.trim() },
            )
          } else {
            setEditing(false)
          }
        }}
      >
        <input
          value={editText}
          onChange={(e) => setEditText(e.target.value)}
          className="flex-1 text-sm bg-transparent outline-none text-[var(--text)]"
          autoFocus
          onKeyDown={(e) => { if (e.key === 'Escape') setEditing(false) }}
        />
        {canReassign && groups.length > 0 && (
          <SimpleSelect
            options={groups.map((g) => g.id)}
            optionLabels={groups.map((g) => g.name)}
            value={editGroup}
            onChange={setEditGroup}
            clearLabel={i18nT('apps.personalShopper.preferencesTab.no_group')}
            aria-label={i18nT('apps.personalShopper.preferencesTab.assign_to_group')}
          />
        )}
        <Btn type="submit" disabled={!editText.trim() || !changed}>{i18nT('apps.personalShopper.preferencesTab.save')}</Btn>
        <button
          type="button"
          onClick={() => setEditing(false)}
          className="text-[var(--muted)]"
          title={i18nT('apps.personalShopper.preferencesTab.cancel')}
          aria-label={i18nT('apps.personalShopper.preferencesTab.cancel')}
        >
          <X size={14} />
        </button>
      </form>
    )
  }

  return (
    <div className="group flex items-center gap-2 px-3 py-2 rounded-lg bg-[var(--card)] border border-[var(--border)] hover:border-[var(--accent)] transition-colors">
      <span className="flex-1 text-sm text-[var(--text)]">{pref.text}</span>
      {pref.tags
        .filter((t) => t !== sectionGroupId)
        .map((t) => (
          <span
            key={t}
            className="text-[10px] px-2 py-0.5 rounded-full bg-[var(--bg-elevated)] border border-[var(--border)] text-[var(--muted)]"
          >
            {groupNames.get(t) ?? t}
          </span>
        ))}
      <div className="opacity-0 group-hover:opacity-100 flex gap-1 transition-opacity">
        <button
          onClick={startEditing}
          className="text-[var(--muted)] hover:text-[var(--accent)]"
          title={i18nT('apps.personalShopper.preferencesTab.edit')}
          aria-label={i18nT('apps.personalShopper.preferencesTab.edit_named_preference', { text: pref.text })}
        >
          <Pencil size={12} />
        </button>
        <button
          onClick={onDelete}
          className="text-[var(--muted)] hover:text-[var(--danger)]"
          title={i18nT('apps.personalShopper.preferencesTab.delete')}
          aria-label={i18nT('apps.personalShopper.preferencesTab.delete_named_preference', { text: pref.text })}
        >
          <Trash2 size={12} />
        </button>
      </div>
    </div>
  )
}
