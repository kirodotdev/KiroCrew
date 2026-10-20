import { acpBackendName } from '../api/acpBackend'
import { HarnessLogo } from './HarnessLogo'

/** The harness a live session runs on, as the mark Settings → Agent Harness draws for it,
 *  whose accessible name and tooltip are the harness name. */
export default function HarnessMark({ backend, size = 13 }: { backend: string; size?: number }) {
  const name = acpBackendName({ id: backend })
  return (
    <span role="img" aria-label={name} title={name} className="inline-flex shrink-0 opacity-70" data-testid="composer-model-chip-backend">
      <HarnessLogo id={backend} size={size} />
    </span>
  )
}
