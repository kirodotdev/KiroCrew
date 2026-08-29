// Writing Review — page entry point. Deliberately thin: mounts the
// AppApiProvider (required for useChatLauncher in ReviewDetail), the
// per-page state provider, and the layout shell. All state lives in
// ``context.tsx``.
//
// AppApiProvider is required here because builtin pages are NOT wrapped by
// AppHost. Without it, any app-sdk hook (useChatLauncher, useAppApi, etc.)
// throws "useAppApi() must be used inside <AppApiProvider>" at render time.
// Same pattern spec-builder and ops-mission-control use for the same reason.
import { useSearchParams, useNavigate } from 'react-router-dom'

import { AppApiProvider } from '../../app-sdk'
import Workspace from './Workspace'
import { WritingReviewProvider } from './context'

// The chat handoff is the only app-sdk call ReviewDetail makes today, so we
// only need to allowlist the API paths that path uses (empty for chat launch —
// it writes a window global and navigates, no fetches).
const APP_API_PATHS: string[] = []

export default function WritingReviewPage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()

  // A finished-review notification can deep-link us to ?review=<id>. Read
  // once for the initial selection only; after mount we let the user
  // navigate freely without the URL fighting them.
  const initialReviewId = searchParams.get('review')

  return (
    <div className="relative h-full">
      <AppApiProvider
        appName="writing-review"
        allowedApiPaths={APP_API_PATHS}
        allowedEvents={[]}
        subscribeFn={() => () => {}}
        navigateFn={(path) => navigate(path)}
        notifyFn={(message, opts) =>
          window.dispatchEvent(new CustomEvent('mc:notify', { detail: { message, ...opts } }))
        }
      >
        <WritingReviewProvider initialReviewId={initialReviewId}>
          <Workspace />
        </WritingReviewProvider>
      </AppApiProvider>
    </div>
  )
}
