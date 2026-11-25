---
description: Draw one GitHub issue as an issue card widget in a chat answer
always: false
---

# Issue card widget

When an answer names a GitHub issue and its details are known, draw it as an
issue card instead of writing HTML:

```html
<mcwidget use="issue-cards:issue-card" title="Issue #40452">
{"number": 40452, "title": "Render chart fences", "state": "open", "labels": ["tier:T3"], "url": "https://github.com/kirodotdev/KiroCrew/issues/40452"}
</mcwidget>
```

The body is one JSON object with these fields:

- `number`: the issue number.
- `title`: the issue title.
- `state`: `open` or `closed`.
- `labels`: optional, at most 8 label names.
- `url`: optional, the issue's `https://` address.

Write only the data. The app draws the card, and a reader without the app sees
the data in a fallback card.
