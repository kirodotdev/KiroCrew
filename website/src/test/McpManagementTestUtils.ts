import { vi } from 'vitest'

import { api } from '../api/client'

export function answerBatchPreviewsPerName() {
  return vi.spyOn(api, 'mcpGatewayLaunchPreviews').mockImplementation((async (names: string[]) => {
    const launches: Record<string, unknown> = {}
    const errors: Record<string, string> = {}
    for (const name of names) {
      try {
        const preview = await api.mcpGatewayLaunchPreview(name)
        if (preview) launches[name] = preview
      } catch {
        errors[name] = 'launch_resolve_failed'
      }
    }
    return { launches, errors }
  }) as never)
}
