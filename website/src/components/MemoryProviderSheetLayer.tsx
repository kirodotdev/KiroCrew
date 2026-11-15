import { MemoryProviderSheetHost } from './MemoryProviderSheet'
import { MemoryMoveSheetHost } from './MemoryMoveSheet'
import { MemoryBenchSheetHost } from './MemoryBenchSheet'

/** The three sheets an app's memory verbs open. Loaded on demand by
 *  `MemoryProviderSheetHosts`, only while the memory provider preview runs. */
export default function MemoryProviderSheetLayer() {
  return (
    <>
      {/* The sheet an app's `requestMemoryProviderChange()` opens; only the owner's
          press there switches which memory Crew uses. */}
      <MemoryProviderSheetHost />
      {/* The sheet an app's `requestMemoryMove()` opens; only the owner's press there
          runs a move. */}
      <MemoryMoveSheetHost />
      <MemoryBenchSheetHost />
    </>
  )
}
