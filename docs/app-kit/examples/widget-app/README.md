# Issue Cards: an App Widget Example

An app with no backend and no page. It declares one widget,
`contributes.widgets[issue-card]`, and one skill that tells agents how to draw
it. Once the app is enabled, an agent in any chat can draw an issue card:

```html
<mcwidget use="issue-cards:issue-card" title="Issue #7">
{"number": 7, "title": "Example", "state": "open"}
</mcwidget>
```

The module is `ui/widgets/issue-card.mjs`. It runs in the sandboxed app frame
with no capability and no network images, and sets every payload value with
`textContent`. See
[`contributes.widgets`](../../manifest-reference.md#contributeswidgets--views-any-chat-can-draw)
for the contract.

## Install

```bash
kirocrew app install ./widget-app
kirocrew app enable issue-cards
```

Trust the app before you enable it, as for every app that is not built in. See
[Third-party executable code is off by default](../../publishing-guide.md#third-party-executable-code-is-off-by-default).
