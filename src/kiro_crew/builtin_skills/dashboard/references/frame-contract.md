# The frame contract: the details

Moved verbatim from the dashboard `SKILL.md` core (*5. The frame contract*).

## 5. The frame contract

The iframe has `allow-scripts` only, an opaque origin, and no access to the
host's cookies, storage or DOM. If the owner's network policy withholds script,
a `<noscript>` explanation can say that the view needs it. Start script-filled
regions hidden and reveal them after drawing; an unfilled placeholder is not
evidence about the person's work.

Values arrive in three ways:

1. `data-dashboard-field="name"` fills an element with text. Useful for a
   scalar cell; arrays and objects appear as raw JSON through this path.
2. `window.kirocrew.fields` holds the same declared values, with `agentic`,
   `seq` and `missing` beside them.
3. `window.kirocrew.subscribe(name, cb)` calls `cb(value, name, meta)`
   immediately and on each push that changes the field. Its returned function unsubscribes. A chart or
   script-only field needs no extra text binding or hidden sink. `meta` is
   `{missing}`; read the agentic marks from `window.kirocrew.agentic` and
   the write times from `window.kirocrew.written_at`. When a field you drew
   stops resolving, `cb` runs again with the value you already drew and `meta.missing === true`; mark
   that drawing as not current, and keep the value. It runs once more with
   `missing: false` when the field resolves. A field that never resolved does
   not call back. So a callback can receive a value it already had: a page
   that appends each call (a time series, a log) checks `meta.missing` or
   compares with its last value before adding a point.

Subscribe during parse. The subscription survives a replaced fields object;
zero is a present value and still reaches the callback. A failing callback
leaves the others running. The `kirocrew-dashboard` event is also available,
but only announces a refill rather than naming the field. It fires only when the
read changed, and at most once a minute on a read that changed nothing, so a
page drawing elapsed time from the clock stays current.
Redraw on this event, not on every `message`.

Use `createElement` and `textContent` for recorded text. Inserting recorded
strings as `innerHTML` would turn somebody's data into page markup. Numerical
geometry comes from validated numbers. Bindings and graphics read the same
source, and labels say which subset they describe.

The host retains the last resolved field values, dims a cell named in
`missing` and labels it "not current", and shows the person no band; a field that does not resolve is
reported to you in your `[DASHBOARD]` turn block. Draw no freshness or
staleness badge of your own beyond marking what `meta.missing` names. Preserve the distinction rather than replacing an unread
value with a made-up zero. Your words and graphics agree with the folded
numbers; derive changing headlines from those values too.
