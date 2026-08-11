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

// ── Preference Row ──

function PreferenceRow({
  pref,
  onDelete,
  onUpdate,
  groupNames,
  sectionGroupId,
}: {
  pref: Preference
  onDelete: () => void
  onUpdate: () => void
  /** Group id -> display name. Tag ids are opaque, so without this the row
   *  renders meaningless hex where the group's name belongs. */
  groupNames: Map<string, string>
  /** The group whose section this row is rendered in, if any. Its own pill is
   *  pure restatement of the heading directly above it. */
  sectionGroupId?: string
}) {
  const [editing, setEditing] = useState(false)
  const [editText, setEditText] = useState(pref.text)

  const updateMutation = useMutation({
    mutationFn: (text: string) => updatePreference(pref.id, { text }),
    onSuccess: () => { setEditing(false); onUpdate() },
  })

  if (editing) {
    return (
      <form
        className="flex items-center gap-2 px-3 py-2 rounded-lg bg-[var(--card)] border border-[var(--accent)]"
        onSubmit={(e) => {
          e.preventDefault()
          if (editText.trim() && editText !== pref.text) {
            updateMutation.mutate(editText.trim())
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
        <Btn type="submit">{i18nT('apps.personalShopper.preferencesTab.save')}</Btn>
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
          onClick={() => setEditing(true)}
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
