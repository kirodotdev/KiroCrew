import type { ReactNode } from 'react'
import { Building2, Brain, Wand2, Waves, Rocket, Sparkles, TreePine, Ghost } from 'lucide-react'
import { i18nT } from '../../i18n/t'

export type SceneKey = 'office' | 'panda' | 'neural' | 'wizard' | 'underwater' | 'mission' | 'serengeti' | 'ghost'

export interface SceneMeta {
  key: SceneKey
  label: string
  icon: ReactNode
  desc: string
}

// Getters: resolve in the active language at read time.
export const SCENES: SceneMeta[] = [
  {
    key: 'office',
    get label() { return i18nT('pages.scenes.config.office_label') },
    icon: <Building2 className="lucide-inline" />,
    get desc() { return i18nT('pages.scenes.config.office_desc') },
  },
  {
    key: 'panda',
    get label() { return i18nT('pages.scenes.config.panda_label') },
    icon: <Sparkles className="lucide-inline" />,
    get desc() { return i18nT('pages.scenes.config.panda_desc') },
  },
  {
    key: 'neural',
    get label() { return i18nT('pages.scenes.config.neural_label') },
    icon: <Brain className="lucide-inline" />,
    get desc() { return i18nT('pages.scenes.config.neural_desc') },
  },
  {
    key: 'wizard',
    get label() { return i18nT('pages.scenes.config.wizard_label') },
    icon: <Wand2 className="lucide-inline" />,
    get desc() { return i18nT('pages.scenes.config.wizard_desc') },
  },
  {
    key: 'underwater',
    get label() { return i18nT('pages.scenes.config.underwater_label') },
    icon: <Waves className="lucide-inline" />,
    get desc() { return i18nT('pages.scenes.config.underwater_desc') },
  },
  {
    key: 'mission',
    get label() { return i18nT('pages.scenes.config.mission_label') },
    icon: <Rocket className="lucide-inline" />,
    get desc() { return i18nT('pages.scenes.config.mission_desc') },
  },
  {
    key: 'serengeti',
    get label() { return i18nT('pages.scenes.config.serengeti_label') },
    icon: <TreePine className="lucide-inline" />,
    get desc() { return i18nT('pages.scenes.config.serengeti_desc') },
  },
  {
    key: 'ghost',
    get label() { return i18nT('pages.scenes.config.ghost_label') },
    icon: <Ghost className="lucide-inline" />,
    get desc() { return i18nT('pages.scenes.config.ghost_desc') },
  },
]

export const SCENE_STORAGE_KEY = 'mc-agent-scene'

/** CSS layout multiplier — keeps containers the same size as the original S=3 era */
export const SCENE_LAYOUT_SCALE = 3

/** Canvas pixel-buffer multiplier — sharp on HiDPI screens */
export const SCENE_SCALE = SCENE_LAYOUT_SCALE * Math.min(Math.ceil(window.devicePixelRatio ?? 1), 2)

export const POPOUT_CHANNEL = 'kirocrew-worlds-popout'
