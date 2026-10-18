/**
 * CrewEditorModal — the crewmate editor entry point. It mounts the redesigned
 * CrewEditorNextDialog, which is the single editing experience; the earlier
 * A/B toggle and the classic dialog have been retired.
 *
 * It is driven by the CrewEditorController the page builds with useCrewEditor.
 * This component is the lazy-split boundary MembersPage imports, so the editor
 * code stays off the main App chunk exactly as the classic lazy import did.
 */
import CrewEditorNextDialog from './CrewEditorNextDialog'
import type { CrewEditorController } from './useCrewEditor'

export default function CrewEditorModal({ ctl }: { ctl: CrewEditorController }) {
  return <CrewEditorNextDialog ctl={ctl} />
}
