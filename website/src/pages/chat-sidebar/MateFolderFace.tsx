/**
 * A mate's home folder wears the mate's face instead of the folder
 * glyph. The gateway marks the folder `mate_home` and owns it as
 * `member:<store>`; the face is the roster row on that memory store. A folder
 * the roster does not match (the read is loading or failed, the mate was
 * deleted) draws the plain glyph.
 */
import { useQuery } from '@tanstack/react-query'

import { membersRosterQuery } from '../../api/membersQuery'
import CrewAvatar from '../../components/CrewAvatar'
import FolderGlyph from '../../components/FolderGlyph'
import type { ChatFolder } from '../../types'

const MEMBER_PREFIX = 'member:'

/** The memory store of the mate whose home folder this is, else ''. */
export const mateStoreOfFolder = (folder: ChatFolder): string =>
  folder.mate_home && folder.owner_app?.startsWith(MEMBER_PREFIX) ? folder.owner_app.slice(MEMBER_PREFIX.length) : ''

function PlainGlyph({ folder, open, className, testId }: FaceProps) {
  return <FolderGlyph color={folder.color} icon={folder.icon} size={14} open={open} className={className} testId={testId} />
}

interface FaceProps {
  folder: ChatFolder
  open: boolean
  className?: string
  testId?: string
}

function MateFace(props: FaceProps & { store: string }) {
  const { data } = useQuery(membersRosterQuery)
  const row = Array.isArray(data) ? data.find(r => r.memory_store === props.store) ?? null : null
  if (!row) return <PlainGlyph {...props} />
  return (
    <span data-testid={props.testId} data-mate-folder={row.name} aria-hidden="true" className="shrink-0 inline-flex">
      <CrewAvatar seed={row.name} avatar={row.avatar} size={14} working={row.running ? 'subtle' : undefined} />
    </span>
  )
}

export { isMateManaged } from '../../utils/mateFolders'

export function MateFolderFace(props: FaceProps) {
  const store = mateStoreOfFolder(props.folder)
  // The roster query registers only on a mate's folder row.
  return store ? <MateFace {...props} store={store} /> : <PlainGlyph {...props} />
}
