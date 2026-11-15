# Show issues in your app

`@kirocrew/app-sdk` draws issue lists with one API. Your app passes a query; the
gateway reads the provider with the user's own sign-in and answers in one shape.
The route serves GitHub, GitLab, Jira and Linear. The full reference is
[api-reference.md § Issue List](api-reference.md#issue-list).

## 1. Declare the endpoint

Add the list endpoint to `permissions.api` in `app.json`:

```json
"permissions": { "api": ["/api/source/issues"] }
```

Without it the components show a refusal naming the path and send nothing.
`/api/source/issue` (the single-issue read) does not cover the list.

## 2. Show a list

```tsx
import * as sdk from '@kirocrew/app-sdk'

function MyIssues() {
  if (!(sdk.APP_SDK_LEVEL >= 6 && sdk.IssueList)) return <p>Update Kiro Crew to see issues.</p>
  return (
    <sdk.IssueList
      query={{ provider: 'github', scope: 'acme/widgets', assignee: 'me' }}
      view="condensed"
      showFilters
      onSelect={issue => console.log(issue.key)}
    />
  )
}
```

- `scope` is a GitHub `owner/repo`, a GitLab project path, a Jira project key or a
  Linear team key. `host` selects a GitHub Enterprise or self-managed GitLab host on
  the gateway's allowlist, and is required for Jira (the site the gateway has
  credentials for).
- `view="condensed"` draws one row per issue. `view="expanded"` draws a card
  with the excerpt, people, counts and an **Open in** link.
- `showFilters` adds a bar for state, assignee (`me` included), labels and search
  text. Each edit starts again from the first page.
- **Load more** follows the response's `nextCursor` and disappears on the last
  page.

Import the SDK as a namespace when you support hosts older than level 6: a named
import of an export the host lacks fails your whole bundle at load time.

## 3. Build your own layout

`useIssues(query)` returns the same data without any UI:

```tsx
const { issues, isLoading, error, hasNextPage, fetchNextPage } = sdk.useIssues({
  provider: 'github', scope: 'acme/widgets', labels: ['bug'],
})
```

`IssueCard` draws one issue on its own, in either view, so a board or a detail
column can reuse it: `<sdk.IssueCard issue={issues[0]} view="expanded" />`.

## What the components guarantee

- Issue text (titles, excerpts, labels, names) is rendered as text, never HTML.
- Links open only `http:` or `https:` urls, in a new tab with
  `rel="noopener noreferrer"`. Avatars load only from `http(s)` urls.
- The views use the dashboard theme, work at 320px wide, and every row is
  reachable by keyboard.
