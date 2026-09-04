import { compareText } from '../i18n/format'
/**
 * Multi-select skill picker for the Hooks form.
 * Read-only chips + single "Add" trigger; add/remove live in the dropdown.
 */
import { useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Brain, ChevronDown, Plus } from 'lucide-react'
import { api } from '../api/client'
import { Btn } from './ui'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { i18nT } from '../i18n/t'
import HookSkillsDropdown from './HookSkillsDropdown'

interface CatalogSkill {
  key: string
  name: string
  description?: string
}

interface Props {
  selected: string[]
  onChange: (skills: string[]) => void
}

export default function SkillsMultiSelect({ selected, onChange }: Props) {
  const btnRef = useRef<HTMLButtonElement>(null)

  const { data: catalog = [] } = useQuery<CatalogSkill[]>({
    queryKey: ['skills'],
    queryFn: async () => {
      const rows = await api.skills()
      return Array.isArray(rows) ? (rows as CatalogSkill[]).filter(s => s?.key) : []
    },
    staleTime: 60_000,
  })

  const byKey = useMemo(() => {
    const m = new Map<string, CatalogSkill>()
    for (const s of catalog) m.set(s.key, s)
    return m
  }, [catalog])

  const candidates = useMemo(
    () => catalog.filter(s => !selected.includes(s.key)).sort((a, b) => compareText(a.name, b.name)),
    [catalog, selected],
  )

  const { open, setOpen, filter, setFilter, dropdownRef, inputRef, filtered } =
    useFilteredDropdown(candidates)

  const add = (key: string) => { onChange([...selected, key]) }
  const remove = (key: string) => { onChange(selected.filter(k => k !== key)) }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-1.5 min-h-[32px]">
        {selected.map(key => {
          const skill = byKey.get(key)
          return (
            <span
              key={key}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[12px] font-mono bg-accent-subtle border border-accent/30 text-text"
              title={skill?.description || key}
            >
              <Brain className="lucide-inline" />
              {skill?.name || key.split('/').pop()}
            </span>
          )
        })}
        <Btn
          ref={btnRef}
          className="flex items-center gap-1 px-2 py-0.5 text-[12px]"
          onClick={() => setOpen(!open)}
        >
          <Plus className="lucide-inline" /> {i18nT('components.skillsMultiSelect.add_skill')}
          <ChevronDown className="lucide-inline text-muted" />
        </Btn>
      </div>
      {open && (
        <HookSkillsDropdown
          anchorRef={btnRef}
          dropdownRef={dropdownRef}
          inputRef={inputRef}
          filter={filter}
          setFilter={setFilter}
          onClose={() => setOpen(false)}
          selected={selected}
          filtered={filtered}
          byKey={byKey}
          onAdd={add}
          onRemove={remove}
        />
      )}
      <p className="text-[11px] text-muted mt-1">{i18nT('components.skillsMultiSelect.skills_hint')}</p>
    </div>
  )
}
