import type { AgentSource } from '../../hooks/useAgentSync'
import type { SceneKey } from './config'
import OfficeScene from './OfficeScene'
import PandaOfficeScene from './PandaOfficeScene'
import NeuralConstellationScene from './NeuralConstellationScene'
import WizardTowerScene from './WizardTowerScene'
import UnderwaterLabScene from './UnderwaterLabScene'
import MissionControlScene from './mission-control/MissionControlScene'
import WateringHoleScene from './WateringHoleScene'
import GhostScene from './GhostScene'

export const SCENE_COMPONENTS: Record<SceneKey, React.ComponentType<{ agents: AgentSource[]; visible?: boolean }>> = {
  office: OfficeScene, panda: PandaOfficeScene, neural: NeuralConstellationScene, wizard: WizardTowerScene,
  underwater: UnderwaterLabScene, mission: MissionControlScene,
  serengeti: WateringHoleScene, ghost: GhostScene,
}
