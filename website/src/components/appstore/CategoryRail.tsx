/**
 * CategoryRail — left rail of the Discover "All apps" section.
 *
 * Two blocks:
 *  - CATEGORIES: canonical categories with app counts; selecting one filters
 *    the list ("All apps" resets).
 *  - SOURCES: where apps come from (trust provenance) — Built-in plus each
 *    configured external registry with its app count. Selecting a source filters
 *    the list; All sources clears only that filter. Add source opens the popover.
 */
import { BadgeCheck, Database, Plus, ShieldCheck, Users } from 'lucide-react'
import type { Category } from './categories'
import Clickable from '../Clickable'
import { sourceRowKey } from './types'

import { i18nT } from '../../i18n/t'
/**
 * One SOURCES row.
 *
 * `name` is the raw registry id (or a first-party sentinel); `sourceRowKey`
 * namespaces external ids for counts and selection. `label` is only what is
 * shown. `review`
 * carries the registry's review tier so the row can say how thoroughly its
 * listings were vetted; it is display-only and says nothing about whether the
 * apps clone with the user's credentials.
 */
export type SourceRow = { name: string; label: string; count: number; builtin: boolean; review?: string }

/**
 * Hover text for a source row: the review claim, or nothing.
 *
 * A row with no review tier gets NO title rather than a generic one — an
 * unreviewed source has nothing to say, and inventing reassuring text for it
 * would be the over-claim this whole change exists to remove.
 */
function sourceTitle(s: SourceRow): string | undefined {
  if (s.review === 'curated') return i18nT('components.appstore.categoryRail.curated_reviewed_by_the_kiro_crew_team')
  if (s.review === 'community') return i18nT('components.appstore.categoryRail.community_listed_not_vetted_by_the_kiro_crew_team')
  return undefined
}

/**
 * The VISIBLE one-word tier for a source row, or `''`.
 *
 * The claim cannot live in a `title` and an `aria-label` alone. A touch user
 * has no hover: "not vetted" must remain visible on the existing count line.
 * The source row's filter action never changes what its review badge claims.
 *
 * The words come from `components.appstore.registryTier`, the same keys the
 * External Registries card's badges read, so one tier can never be named two
 * things across the two surfaces.
 */
function sourceTier(s: SourceRow): string {
  if (s.review === 'curated') return i18nT('components.appstore.registryTier.curated')
  if (s.review === 'community') return i18nT('components.appstore.registryTier.community')
  return ''
}

export default function CategoryRail({ categories, total, selected, onSelect, sources, selectedSource, onSelectSource, onAddSource }: {
  categories: { category: Category; count: number }[]
  total: number
  selected: Category | 'all'
  onSelect: (c: Category | 'all') => void
  sources: SourceRow[]
  selectedSource: string | null
  onSelectSource: (source: string | null) => void
  onAddSource: () => void
}) {
  const item = (label: string, count: number, key: Category | 'all') => {
    const on = selected === key
    return (
      <button
        key={key}
        type="button"
        aria-pressed={on}
        className={`w-full flex items-center justify-between px-2.5 py-1.5 rounded-lg text-[13px] text-left cursor-pointer border-0 bg-transparent transition-colors ${
          on ? 'bg-[var(--accent-subtle)] text-text-strong font-semibold' : 'text-text hover:bg-bg-hover'
        }`}
        onClick={() => onSelect(key)}
      >
        {label} <span className="text-muted text-[11.5px] font-normal">{count}</span>
      </button>
    )
  }

  return (
    <div className="flex flex-col gap-[18px] w-full">
      <div>
        <div className="text-[11px] font-bold tracking-[.1em] text-muted mb-2">{i18nT('components.appstore.categoryRail.categories')}</div>
        {item(i18nT('components.appstore.categoryRail.all_apps'), total, 'all')}
        {categories.map(({ category, count }) => item(category, count, category))}
      </div>
      <div>
        <div className="text-[11px] font-bold tracking-[.1em] text-muted mb-2">{i18nT('components.appstore.categoryRail.sources')}</div>
        <Clickable
          aria-pressed={selectedSource === null}
          className={`focus-ring w-full flex items-center justify-between px-2.5 py-1.5 rounded-lg text-[13px] cursor-pointer mb-1.5 ${selectedSource === null ? 'bg-[var(--accent-subtle)] text-text-strong font-semibold' : 'text-text hover:bg-bg-hover'}`}
          onClick={() => onSelectSource(null)}
        >
          {i18nT('appStoreSources.all')}
        </Clickable>
        {sources.map(s => (
          <Clickable
            key={sourceRowKey(s)}
            aria-pressed={selectedSource === sourceRowKey(s)}
            onClick={() => onSelectSource(sourceRowKey(s))}
            /* The whole row carries the tooltip, not just the icon: the icon is a
               14px glyph and a hover target that small is easy to miss, while the
               review claim is the thing a user needs before installing. */
            title={sourceTitle(s)}
            className={`focus-ring cursor-pointer flex items-center gap-2 px-2.5 py-[7px] border rounded-[9px] text-[12.5px] mb-1.5 ${selectedSource === sourceRowKey(s) ? 'border-accent bg-[var(--accent-subtle)] text-text-strong' : 'border-border bg-card text-text hover:border-border-strong'}`}
          >
            {/* ONE icon per tier, the same glyphs the External Registries card
                uses, so the same source is not drawn two ways across the two
                surfaces. `BadgeCheck` stays reserved for FIRST-PARTY: giving it to
                a curated registry too blurred built-in against
                team-reviewed-external, which are different claims. Curated gets
                the card's shield, community the card's `Users` in muted ink so it
                never reads as endorsed, and anything else keeps today's neutral
                Database. The icon is labelled, not decorative, because on a
                community row it is the only mark distinguishing the row. */}
            {s.builtin
              ? <BadgeCheck size={14} className="text-accent shrink-0" aria-label={i18nT('components.appstore.categoryRail.first_party')} />
              : s.review === 'curated'
                ? <ShieldCheck size={14} className="text-accent shrink-0" aria-label={i18nT('components.appstore.categoryRail.curated_reviewed_by_the_kiro_crew_team')} />
                : s.review === 'community'
                  ? <Users size={14} className="text-muted shrink-0" aria-label={i18nT('components.appstore.categoryRail.community_listed_not_vetted_by_the_kiro_crew_team')} />
                  : <Database size={14} className="text-muted shrink-0" />}
            <div className="min-w-0">
              <div className="text-text truncate" title={s.label}>{s.label}</div>
              <div className="text-muted text-[11px] truncate">
                {/* The tier is VISIBLE, ahead of the count: a hover title cannot
                    reach a touch or keyboard user, and this is the line they read
                    before installing. `warn` ink on the community tier so it does
                    not read as another neutral fact about the row. */}
                {sourceTier(s) && (
                  <span className={s.review === 'community' ? 'text-warn' : 'text-accent'}>
                    {sourceTier(s)}
                    {' · '}
                  </span>
                )}
                {i18nT('components.appstore.categoryRail.app', { count: s.count })}
              </div>
            </div>
          </Clickable>
        ))}
        <button
          type="button"
          className="flex items-center gap-1.5 text-[12.5px] text-accent cursor-pointer border-0 bg-transparent px-0.5 py-1 hover:underline"
          onClick={onAddSource}
        >
          <Plus size={13} /> {i18nT('components.appstore.categoryRail.add_source')}
        </button>
      </div>
    </div>
  )
}
