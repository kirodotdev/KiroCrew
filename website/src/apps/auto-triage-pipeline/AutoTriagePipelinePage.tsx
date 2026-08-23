// AutoTriagePipelinePage — the page-entry component the builtin registry lazy-
// loads for the `/auto-triage-pipeline` route. Deliberately thin: it just gives
// the fabric view a full-height surface (a full page, not a narrow panel — the
// wave-2 faithful drawing needs the room). All the work is in `views/PipelineView`
// and the pure, tested `lib/fabric`.
import PipelineView from './views/PipelineView'

export default function AutoTriagePipelinePage() {
  return (
    <div className="h-full min-h-0 overflow-hidden bg-bg">
      <PipelineView />
    </div>
  )
}
