# A slot's newest crew log carries the slot's state, so the older ones expire with the retention setting

Decided by: Bolin Chen (bolichen97), Kiro Crew maintainer
Date: 2026-11-08
Replaces: the rule in [`crew-log-core.md`](../system-specs/modules/crew-log-core.md),
"What the sweep selects", that `destroyed` is the only close reason that lets retention
collect a session's crew log.

## Decision
When a slot moves to a new session id, the gateway first writes every slot fold's state,
the state of the work tree the slot roots, and the slot's session status into the slot's
newest crew log, then closes each older crew log of that slot `superseded`, and the
retention sweep collects a `superseded` crew log once it is older than
`session.archive_retention_days`, the same as a `destroyed` one.

- The state goes forward as one `session/carried` entry per slot fold, one for the
  work tree of the boards the slot roots, and one for the session status a gone
  worker's row reads, and a fold that reads one replaces its state with it. So the
  slot's state is not lost when its older crew logs go: the work ledger, a crew's work
  items and skip index, a crewmate's dashboard values, work tree and refused writes,
  per-task costs, and whether a gone worker reads closed or lost. The history views
  that are not carried, and the other things lost, are under "Costs accepted".
- The work ledger is carried over every crew log its readers join, including the crew
  logs headed with a caller's own spelling of the slot (a `cron:<id>` key, a gone chat's
  key that a workflow's `workflow-<run>` fallback tab runs under, or a Slack
  conversation's raw `slack:<ts>` key an older build used). Each opener records, in the
  new crew log's `session/opened`, the session key the slot's turns run under (the tab's
  effective session key on the dashboard, the conversation's own key on a channel), and
  the carry joins the crew logs headed with that key's ledger spelling. The new crew log
  is this build's own, so the first carry after an upgrade joins them with nothing an
  older build wrote. The carry also joins a spelling the gateway names the slot from
  (the key's filename fold, or a cron job's `cron-<id>` tab), found on the crew logs'
  headers, and any spelling a ledger write or a live read recorded. A new crew log whose
  opening states no session key is not carried, and none of its slot's older crew logs
  is closed. A carry replaces what a joined read folded before it, and each later carry
  folds over the carry before it, so a record keeps reading for a key the slot's first
  carry joined; a later carry that joins a key does not bring back the records the first
  one left out. No tab reaches that case: the gateway links a tab only while it has none
  and never clears a link, so a tab linked when it is created states that key in every
  crew log's opening, and a tab it links after the tab ran (a channel tab, a cron job's
  tab) is named from that key, so every carry joins it from the crew logs' headers, the
  first one included.
- A joined spelling can be a slot with carries of its own. A workflow's fallback tab
  reads under its gone chat's key, so it joins the chat's crew logs, and a reset of the
  chat carried the chat's ledger and closed the chat's older crew log `superseded`. A
  read through the join applies the reading slot's carries, never the chat's, so until
  the reading slot holds a ledger carry of its own it reads the chat's records from the
  chat's crew logs themselves. Three things keep those records reading:
  - A slot's first crew log carries the ledger too when its ledger readers join crew
    logs headed with another spelling, and closes nothing, since the slot has no older
    crew log.
  - The ledger carry folds the crew logs headed with the first joined spelling the way
    that spelling's own ledger does, applying its own carry there, so it holds the
    chat's records however many of the chat's crew logs are left. When a crew log
    headed with a later joined spelling holds a carry of its own, nothing is carried and
    nothing is closed.
  - The sweep keeps a `superseded` crew log that holds a ledger record while another
    slot reads it through the join and holds no ledger carry of its own: a slot one of
    whose crew logs states, in its first opening or in the newest opening of a crew log
    an older build opened, a session key whose ledger spelling is that crew log's slot,
    or a slot the gateway names from that crew log's slot when that slot is a raw caller
    key. It reads that from the crew log tree alone. A `superseded` crew log that holds
    no ledger record is collected whoever reads it, since no ledger read can lose
    anything from it.

  So a record written under the key a slot's new crew log runs under keeps reading for
  a caller under that key after the carry and after retention, and a read under the
  slot's own spelling sees it too from the slot's first carry on. A slot that starts
  joining a spelling after the sweep collected one of that spelling's crew logs reads
  its records from its first carry on, which folds off the writer: a read between that
  slot's first opening and its first carry folds the joined crew logs that are left.
- The close is withheld while a session-map key still maps the old session id, while
  the map cannot be read, and while a turn of the old session is still running. Reading
  the map can only hold a close back, never grant one.
- The sweep still reads nothing but the crew log tree.

## Why
`~/.kiro/crew/crew-log/sessions` grew without bound whatever the retention setting
said: a reset, a discarded conversation, a rewind and a restart whose resume fails each
leave a crew log behind that no close reason let the sweep take. Collecting those on age
alone would have deleted live slot state, because every slot-keyed fold reads every crew
log its slot ran under. Of the three options in the decision brief (carry the state
forward, let old slot state expire, keep every crew log of a live slot), carrying it
forward is the only one that both stops the growth and keeps what the slot's reads show.

## Costs accepted
- A crew log that no later crew log of its slot follows (a closed tab, an archived chat)
  is still kept for as long as before. Whether those should expire is a separate
  decision.
- A crew's `radar` carry needs the crew's recorded unit order, which only the Issue Radar
  crew store can give, and it finds the crew from the slot alone. A crew record outside the
  app's data root, or one the glob cannot name uniquely, carries nothing for its whole
  slot, and that slot's older crew logs are kept until a later carry can read the order.
- A fold state too large for one crew log entry is not carried, and that slot's older
  crew logs are kept.
- The operator's work-board rebuild (`work_ledger.rebuild_from_projection`) also reads
  each worker's own crew logs, for reports made before reports were written into the
  board's crew log. A worker's carry is for the worker's own slot, so once that worker's
  older crew logs are collected a rebuild no longer finds those reports and writes the
  items without them. Reports made since are in the board's crew logs and are carried.
- A board read past its unit limit (the newest 512 crew logs) says how many crew logs it
  left out. The carry holds what the read showed, but once the left-out crew logs are
  collected that count reads zero.
- The context panel's history is not carried. The panel reads the slot's usage over
  every crew log it ran under: the context trend, each past turn's detail, the newest
  turn's breakdown and the fullest turn of the last days. Once the older crew logs are
  collected, the panel covers only the turns of the crew logs still kept, and so do the
  slot-wide token, credit and compaction totals in the same record.
- The root card's tool tally is not carried. The card counts the tool calls of the
  slot's succession chain, and loses the calls of a collected crew log in it. The
  card then reads its list as not whole, so it never says a tool was not called. A
  carry is written for the slot and a reset starts a new chain, so carrying the tally
  would put an earlier conversation's calls into a later one.
- The sweep still runs only when a transcript archive is written, at most once an hour.

## Evidence
- https://github.com/kirodotdev/KiroCrew/issues/23082 -- the issue: crew logs closed by a
  reset or left unclosed by a restart are never collected.
- https://github.com/kirodotdev/KiroCrew/issues/23082#issuecomment-6673256291 -- the
  maintainer's comment that kept `destroyed` as the only collected close and set out the two
  shapes a fix could take, retention per slot or a carry of every slot fold into the slot's
  current crew log. This entry records the second.
- https://github.com/kirodotdev/KiroCrew/pull/34264 -- the pull request that adds this
  entry, opened under the maintainer's account (bolichen97) to fix #23082 with the carry,
  the second of the two shapes the comment above sets out.
