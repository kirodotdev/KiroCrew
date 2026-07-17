import type { ReactNode } from 'react'
import { Building2, Brain, Wand2, Waves, Rocket, Sparkles, TreePine } from 'lucide-react'

export type SceneKey = 'office' | 'panda' | 'neural' | 'wizard' | 'underwater' | 'mission' | 'serengeti'

export interface SceneMeta {
  key: SceneKey
  label: string
  icon: ReactNode
  desc: string
}

export const SCENES: SceneMeta[] = [
  { key: 'office', label: 'Office', icon: <Building2 className="lucide-inline" />, desc: 'Classic pixel office' },
  { key: 'panda', label: 'Panda Den', icon: <Sparkles className="lucide-inline" />, desc: 'Bamboo forest workspace, all pandas' },
  { key: 'neural', label: 'Neural Net', icon: <Brain className="lucide-inline" />, desc: 'Constellation map' },
  { key: 'wizard', label: 'Wizard Tower', icon: <Wand2 className="lucide-inline" />, desc: 'Alchemy lab' },
  { key: 'underwater', label: 'Deep Lab', icon: <Waves className="lucide-inline" />, desc: 'Underwater station' },
  { key: 'mission', label: 'Mission Control', icon: <Rocket className="lucide-inline" />, desc: 'NASA ops center' },
  { key: 'serengeti', label: 'Watering Hole', icon: <TreePine className="lucide-inline" />, desc: 'Serengeti savanna with giraffes, warthogs, and elephants' },
]

export const SCENE_STORAGE_KEY = 'mc-agent-scene'

/** CSS layout multiplier — keeps containers the same size as the original S=3 era */
export const SCENE_LAYOUT_SCALE = 3

/** Canvas pixel-buffer multiplier — sharp on HiDPI screens */
export const SCENE_SCALE = SCENE_LAYOUT_SCALE * Math.min(Math.ceil(window.devicePixelRatio ?? 1), 2)

export const POPOUT_CHANNEL = 'kirocrew-worlds-popout'
