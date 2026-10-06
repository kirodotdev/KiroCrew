---
name: dashboard-template
description: "Kiro Crew repo only: load this when a task will author or change a dashboard page. A page never ships alone: the same PR carries its TypedDict contract, its provider, and a parity test that the two agree. Scaffold all four from one field list; read numbers from an existing fold, never type them."
triggers: dashboard template, dashboard card, data-dashboard-field, template contract, card contract, dashboard html, panel template, new dashboard, scaffold template
---

# Dashboard Template

A dashboard page is HALF of a template. The other half is a type, and the two halves
are checked by different machines, which is why they drift.

> This skill is the procedure for the `kirocrew-dashboard-author` crewmate, whose spec
> is `agent-spec.md` beside this file: the charter (what it is for and what it may never
> do) plus the narrowed toolset that makes the charter true against a tool list rather
> than only against prose. A human or another agent following this file gets the same
> procedure. The spec is NOT yet registered as an installed agent (no installer or
> owned-spec entry ships it; see `docs/system-specs/modules/dashboard-templates.md`), so
> today nobody starts with this loaded: load this skill yourself.

The page is inert html. The host sanitizes it, renders it, and binds text into it by
`data-dashboard-field`. No type checker is anywhere near that binding, because one half
is markup. So a page that reads `settled` while its provider fills `settled_count`
renders an empty cell, on a status board, forever, with nothing red anywhere.

Read the host before you write a page; it is short and it is the authority on what gets
refused. `src/kiro_crew/dashboard/dynamic_cards.py` holds `normalize_card`, which decides
whether a card is accepted at all, and
`website/src/pages/chat/command-center/dashboardDocument.ts` is what queries
`[data-dashboard-field]` and writes the text in.

**You are done when four files and one registration line exist, not when the page looks
right.** Everything here is dev time: it lands through a pull request, mypy is blocking
and the parity test is a gate. At run time the gateway evaluates none of it. A publisher
supplies three sentences and nothing else.

## What a run produces

| # | File | What it is |
|---|---|---|
| 1 | `<slug>.html` | the page: inert layout, one `data-dashboard-field` per value, zero controls |
| 2 | `<slug>_contract.py` | the `TypedDict` naming exactly those fields, plus the judgment type and `CONTRACT_VERSION` |
| 3 | `<slug>_provider.py` | `build_<slug>(view, judgment, *, captured_at)` whose RETURN TYPE is the contract |
| 4 | `test_dashboard_template_<slug>.py` | the parity gate, both directions, plus the planted failures |
| 5 | one line in `registry.py` | `slug -> (contract, provider, source fold, version)` |

Files 1-3 live beside the package's other templates; file 4 goes in the repository's
test directory. The scaffold writes all four and prints line 5 for you to paste.

## Do it in this order

1. **Name the question the page answers, in one sentence a person would ask.** "How much
   of this goal is settled, and what is waiting on me." Not "a board of metrics". If you
   cannot write the sentence, you do not yet know which fold to read.
2. **Pick the fold.** Take one of the ten advertised folds in `FOLDS.md` (not the
   *(internal)* `class` fold, which `--fold` still accepts). A new fold is the exception and needs
   its own justification section in the pull request body, because a fold is a durable
   projection of the append-only log and every reader pays for it.
3. **Write the field list.** One token per value: `name:str|unsaid` for text, or
   `name:fraction:<total>` for a count, naming the fold key that holds its total. Both halves are checked against the fold, and the numerator is read under the field's own name -- add a fourth token, `shown:fraction:calls:open`, when the card field is deliberately named something else. A text field takes the same escape as one optional third token, `shown:str|unsaid:<fold_key>`. There is no plain `str` kind: every declared field is
   read out of the fold, and a fold value can always be absent, so a text field is
   `str | Unsaid` or it is a contract the provider cannot satisfy. (`contract_version`
   and `captured_at` are plain `str` because the provider writes them; the scaffold adds
   them itself.) Keep the whole card at 24 fields or fewer, the five the scaffold adds
   (`contract_version`, `captured_at`, `lede`, `you`, `notes`) included, so at most 19 of yours; the host refuses an over-cap card WHOLE, so a
   page one field too large stops appearing rather than degrading.
4. **Run the scaffold.** It emits the four files from that one list, so the four cannot
   disagree about what the fields are.
5. **Read the fold and fill the derivations.** The scaffold guesses that a field named
   `x` is read from the fold's `x`. Where the fold spells it differently, or where the
   value is a count over a list, edit the provider. Only the provider.
6. **Make the page worth reading.** The scaffold's html is correct and plain: a label and
   a value per row. Lay it out properly, group what belongs together, use the theme
   variables. Do not add a binding without adding the contract key in the same edit.
7. **Register it,** run the gates, open the pull request.

```bash
python3 <this skill>/scripts/scaffold.py <slug> \
  --fold status \
  --fields turns_refused:fraction:turns_completed lifecycle:'str|unsaid'
```

`--print-only` shows the paths and writes nothing. `--force` overwrites, and the output
is deterministic, so re-running after a field-list change gives a reviewer a diff of
exactly that change.

## The three invariants

These are tests, not advice. Each one exists because its absence produced a wrong page
that nothing reported.

**1. The page and the contract read the same field set, both directions.** Strict
equality, no exemption list. An exemption list is a hiding place: a field inconvenient
to render gets declared and listed, and every check still passes. A field the page reads
and the contract omits is an empty cell nobody owns. A key the contract declares and the
page never reads is a provider deriving something nobody sees.

**2. Every number carries its denominator.** `7` on a status page reads as complete
information and is not: a reader cannot tell 7 of 7 from 7 of 700. So a count reaches the
page as `N/M`. That is what the `fraction` kind is, and it is the only way the scaffold
will emit a number. A hand-written contract may declare a bare `int` where the template
genuinely wants the pair rendered apart, and the gate then requires its `<name>_of`
sibling.

**3. A value nobody supplied says so, in words.** The host renders a field it was given
by setting that element's text, and a field it was NOT given as the empty string. On a
page of numbers a blank and a zero are the same reading. So a value that may be unknown
is typed `str | Unsaid`, the provider writes the sentinel out explicitly, and the single
exit turns it into the words "not said". mypy then refuses both the missing key and a
stray `None` from a lookup that found nothing.

The corollary that catches people: **never read a fold with `value.get(key, 0)`**. That
converts "this fold does not carry that" into a number at the first line of the provider.
Use the package's `read_int` and `read_text`, which answer the sentinel for absent,
wrong-typed and unparsable alike — including `bool`, which passes `isinstance(x, int)`
and would render a flag as `1`.

## What the page may not contain

The host's sanitizer strips fourteen of the seventeen outright, so authoring one of those
ships a layout with a hole in it and an author who believes the button exists. It keeps
`label`, `fieldset` and `option`; this package refuses those three itself, because a
dashboard states facts rather than offering a choice.

No `form`, `input`, `button`, `textarea`, `select`, `option`, `label`, `fieldset`,
`script`, `iframe`, `object`, `embed`, `link`, `meta`, `base`, `template`, `noscript`.
That list restates `CONTROL_TAGS` in the package's `parity.py`, which is the authority
and is what `control_tags_used` refuses; a test holds this paragraph to it. No `src`, so
no images. No `href` that is not a same-document fragment (`xlink:href` included);
`outbound_references` refuses those. `style`, `details`/`summary`, inline SVG and CSS
are yours -- but put no binding inside the SVG. Whether SVG paints a string depends on the
ancestor chain, not on the element holding it: `text` paints, the same `text` under `defs`
or `clipPath` paints nothing. The reader refuses the whole foreign subtree rather than
guess per tag. Put the value in a sibling `span` positioned over the drawing.

A dashboard states facts and offers no actions. A decision goes through the product's own
question and approval surfaces, which live outside the rendered frame and carry the
identity of the session that owns them. An imitation approval button in your page is a
lie about authority, and it is the one thing a status surface must never tell.

## Close every element where it opens

Write the end tag for every element, including the ones html lets you leave out -- `</li>`,
`</td>`, `</tr>`, `</p>`, `</head>`. The reader refuses a page that omits one, and refuses a
misnested or stray end tag the same way.

This is stricter than html, and the reason is what the reader has to answer: which element
each binding sits inside, because the host assigns `textContent` in document order and an
assignment to an outer bound element detaches an inner one before its turn comes. Answering
that on markup the browser repairs means reproducing a tree builder, and a reader that gets
one subtly wrong reads a child as a sibling and says so in a precise sentence. So it asks
for markup it can follow instead. Your remedy is always one end tag, on the line the message
names.

## Who may write what

Split on purpose, because the two ends admit of different enforcement.

**The provider derives every number.** It is Python in this tree, so mypy checks it: a
missing key, a misspelled key or a wrong type is a build failure. A number it derives
cannot disagree with the log it summarises, because it read the log.

**A publisher writes three sentences: `lede`, `you`, `notes`.** That is the whole run-time
surface. Numbers are absent from it deliberately — a publisher that can type a count can
make the page contradict the log it claims to summarise. This is not hypothetical: a live
board once rendered an owner's name and a session id in the cell that must say what a
person should DO, and nothing could refuse it. So `you` is gated on phrase SHAPE: a
one-token value is not an action and becomes "not said". No length floor — a floor
rejects nothing a one-token check does not already reject, and it does suppress genuine
short actions like `fix it`, which is the same information loss pointing the other way.

## The fold menu

**Read `FOLDS.md` beside this file.** It is the catalogue: every fold, what it answers,
whether it is keyed by session or by slot, which entry types move it, and the name and
type of every field in its rendered value.

That file is GENERATED from the code by `scripts/fold_catalogue.py`, and a test asserts
the committed copy equals the generated one. So it cannot be stale, which is the only
reason it is safe to trust without opening the projection kernel. Do not edit it; a fold
added or renamed in the product is picked up by `--write`.

Two things to read carefully in it:

- **Keyed by.** A session fold answers about one conversation. A slot fold answers about
  a workstream that outlived several conversations and is folded over every log the slot
  ran under, so serving a slot-wide value under one session's id presents a part as the
  whole.
- **Optional.** A field marked optional is `None` on an empty fold, so a writer could
  leave it unset. Those are exactly the fields your contract types `... | Unsaid` and
  your provider reads with `read_text` / `read_int`. A type of `unknown` means neither
  the empty fold nor any entry declaration says what it holds; treat it as optional text
  and read the fold's own render function before relying on it.

The same catalogue is what `scaffold.py` validates `--fold` against, so a fold the
catalogue does not list is refused before any file is written.

## Choosing a new fold

Only when no existing fold can answer the sentence, and then say so in the pull request
under its own heading:

- the question, and which of the ten you tried and why each fails;
- the entry type the fold reads, and why the log already carries it;
- what it stores, and why that is bounded;
- its `affects` set, which may be wider than the truth and must never be narrower — a
  type wrongly left out drops a real change and serves a stale value with nothing raised.

Adding a fold also moves the stored-state version, which retires every saved checkpoint
to a cold refold. That cost is the reason this is the exception.

## Before you open the pull request

- `mypy` over the source tree. It is the blocking gate and it is what makes the
  provider's return type mean something.
- The package's own gate module and your template's generated test, with the runner
  scoped to those files.
- The formatter and the import sorter over the four new files.
- Confirm the page renders: the contract's key set, the page's binding set, and the
  provider's output keys are all one set. The generated test asserts exactly this, so a
  green run IS the confirmation.

In the body, state which fold you read and why, and paste the page's field list. A
reviewer's first question is always "where did this number come from", and the answer is
the fold name.
