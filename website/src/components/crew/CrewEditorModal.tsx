/**
 * CrewEditorModal — the crewmate editor entry point. It mounts
 * CrewEditorNextDialog, the single crewmate editing experience, on both the
 * Crewmates page and Customize > Crewmates.
 *
 * It is driven by the CrewEditorController the host builds with useCrewEditor.
 * This component is the lazy-split boundary both hosts import, so the editor
 * code stays off the main App chunk.
 */
import CrewEditorNextDialog, { type NextSection } from './CrewEditorNextDialog'
import type { CrewEditorController } from './useCrewEditor'

export type { NextSection as CrewEditorSectionKey }

export default function CrewEditorModal({ ctl, initialSection, onSectionChange }: {
  ctl: CrewEditorController
  initialSection?: NextSection
  onSectionChange?: (section: NextSection | null) => void
}) {
  return <CrewEditorNextDialog ctl={ctl} initialSection={initialSection} onSectionChange={onSectionChange} />
}
