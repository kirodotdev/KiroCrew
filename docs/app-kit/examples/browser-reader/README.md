# Browser Reader — `ctx.browser` example

An app backend that reads and clicks the browser page of one of its own chat slots,
through `ctx.browser` ([api-reference](../../api-reference.md#browser-ctxbrowser)).
No UI: two in-gateway routes.

| Route | Verb | Does |
|---|---|---|
| `GET /api/apps/browser-reader/slots/{slot}/outline` | `read` | the page outline the Bot reads, cut to 2000 characters |
| `POST /api/apps/browser-reader/slots/{slot}/click` with `{"ref": "..."}` | `act` | clicks an element named by a ref from the outline |

`{slot}` is one of this app's own chat slot keys (`browser-reader-<id>`), opened with
`ctx.chat_slots`. Any other key answers `not_your_slot`.

## Install

```bash
kirocrew app install ./browser-reader
```

Then, on the dashboard:

1. Trust and enable the app from the App Store.
2. Turn on **App browser access** in Settings > Developer > Feature previews.
3. Approve `read` and `act` on the app's page. Installing approves neither.

Every answer is a JSON value, never an error page: `{"ok": true, ...}`, or
`{"ok": false, "code": "...", "error": "..."}` with a code such as `preview_off`,
`permission_denied`, `not_your_slot`, `url_blocked` or `user_holds`.
