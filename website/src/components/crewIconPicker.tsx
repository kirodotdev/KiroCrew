/**
 * Settings -> Remote Crew: the picker that changes one crew's icon. Its own module
 * so Settings can load it on demand: the switcher draws icons on the first load,
 * but only this picker needs the localized icon names and the option grid.
 */
import { i18nT } from '../i18n/t'
import {
  CREW_ICONS,
  CREW_ICON_KEYS,
  CrewGlyph,
  crewIconOf,
  isCrewIconKey,
  setCrewIcon,
  useCrewIcons,
  type CrewIconKey,
} from './crewIcons'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
} from './ui/dropdown-menu'
import { IconButton } from './ui'

/** Each icon's catalog key, spelled out so the catalog tooling can see it. */
const ICON_NAME_KEYS = {
  star: 'components.crewIcons.name_star',
  moon: 'components.crewIcons.name_moon',
  sun: 'components.crewIcons.name_sun',
  leaf: 'components.crewIcons.name_leaf',
  flame: 'components.crewIcons.name_flame',
  mountain: 'components.crewIcons.name_mountain',
  anchor: 'components.crewIcons.name_anchor',
  compass: 'components.crewIcons.name_compass',
  feather: 'components.crewIcons.name_feather',
  fish: 'components.crewIcons.name_fish',
  bird: 'components.crewIcons.name_bird',
  cat: 'components.crewIcons.name_cat',
  dog: 'components.crewIcons.name_dog',
  rabbit: 'components.crewIcons.name_rabbit',
  turtle: 'components.crewIcons.name_turtle',
  coffee: 'components.crewIcons.name_coffee',
  gem: 'components.crewIcons.name_gem',
  crown: 'components.crewIcons.name_crown',
  snowflake: 'components.crewIcons.name_snowflake',
  music: 'components.crewIcons.name_music',
} as const satisfies Record<CrewIconKey, string>

/** The localized name of an icon, for the picker's options. */
function iconName(key: CrewIconKey): string {
  return i18nT(ICON_NAME_KEYS[key])
}

/** Settings -> Remote Crew: change one crew's icon. Applies at once, like the crew's on/off switch. */
export function CrewIconPicker({ id, name, disabled }: { id: string; name: string; disabled?: boolean }) {
  const current = crewIconOf(useCrewIcons(), id)
  const Icon = current ? CREW_ICONS[current] : null
  const label = i18nT('components.crewIcons.change_icon', { name })
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <IconButton
          aria-label={current ? i18nT('components.crewIcons.change_icon_current', { name, icon: iconName(current) }) : label}
          disabled={disabled}
          data-testid={`crew-icon-picker-${id}`}
          className="inline-flex items-center justify-center min-w-[28px] min-h-[28px] border border-solid border-border"
        >
          {Icon ? <Icon size={16} aria-hidden /> : <CrewGlyph />}
        </IconButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" aria-label={label}>
        <DropdownMenuRadioGroup
          value={current ?? ''}
          onValueChange={v => {
            if (isCrewIconKey(v)) setCrewIcon(id, v)
          }}
        >
          <div className="grid grid-cols-5 gap-0.5">
            {CREW_ICON_KEYS.map(key => {
              const Option = CREW_ICONS[key]
              const optionName = iconName(key)
              return (
                <DropdownMenuRadioItem
                  key={key}
                  value={key}
                  aria-label={optionName}
                  title={optionName}
                  tile
                  className="w-10 h-10"
                >
                  <Option size={18} aria-hidden />
                </DropdownMenuRadioItem>
              )
            })}
          </div>
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export default CrewIconPicker
