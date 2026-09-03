# Monitor a pull request

Use a pull-request monitor when you want Kiro Crew to keep checking a public
GitHub pull request without spending a model turn on every poll. Kiro Crew wakes
the owning conversation only when a new revision needs action.

## Before you start

A structured monitor supports public `github.com` pull requests whose goal is
`review_ready`. It checks pull-request state, mergeability, review decisions,
unresolved review threads, and check conclusions. Start it from a dashboard,
Slack, or Discord conversation.

Use a finite legacy loop instead when the decision depends on generic comments,
advisory findings, another forge, a deployment, a ticket, or a custom objective.

## Start the monitor

1. Open the conversation that should own the watch.
2. Ask Kiro Crew to monitor the full pull-request URL until it is review-ready.
3. End the turn after the tool reports that application is pending.
4. Confirm the active monitor in the dashboard.

The tool reply is an application request, not proof that the monitor started.
The owning session applies it only after the turn ends. At the start of a later
user turn or monitor wake, the agent can call `monitor_inspect` to read the
authoritative retained state.

Unless you choose different positive limits, the monitor uses:

| Limit | Default |
|---|---:|
| Probe interval | 5 minutes |
| Runtime | 4 hours |
| Completed agent turns | 8 |
| Reported aggregate input and output tokens | 250,000 |
| Consecutive provider errors | 3 |

The token cap applies only to usage reported by the model provider.
`token_usage_known` tells you whether every completed turn included usage. The
runtime and completed-turn limits remain hard fallbacks when usage is unknown.

## Understand when the agent runs

Kiro Crew probes and compares provider facts before it invokes the conversation:

| Observation | Agent turns |
|---|---:|
| No material change | 0 |
| Checks still pending | 0 |
| Provider retry | 0 |
| Review-ready success or terminal blocker | 0 |
| New actionable fingerprint | At most 1 |

An actionable wake includes a compact summary. The agent can then fetch only the
logs, comments, or diff needed for that change. Kiro Crew retains accepted
fingerprints across restart, so the same revision is not handled twice.

Terminal success does not wake the conversation. The dashboard instead reports
success, a terminal blocker, an exhausted budget, or an unavailable owning
session. Use a finite legacy loop when you require a final conversational report
even if the pull request needs no action.

## Inspect or change a monitor

Use `monitor_inspect` on a later turn to see the target, objective, next probe,
limits, latest classification, usage, and final outcome.

You can change the cadence, positive limits, and wake instructions without
discarding the comparison baseline. Changing the target or objective starts a
new baseline and is refused while an action is in flight.

Monitoring never grants extra tool authority. An action turn uses the normal
governance and approval policy of its conversation.

## Stop or restart a monitor

Stop the monitor from the dashboard or ask the agent to stop it. The stop is
durable: inspection retains a `user_stop` outcome instead of deleting the
evidence. Success, provider or authentication blocks, exhausted budgets,
unavailable sessions, and missing completion evidence also retain specific
terminal reasons.

Terminal records are read-only. Choose Restart in the dashboard or ask the agent
to start a new watch.

## Use a finite legacy loop

For unsupported evidence or targets, ask Kiro Crew to run a same-session goal
loop with a positive interval, cycle cap, and runtime limit. Every delivered
cycle is a full agent turn, extends the conversation, and spends model and tool
tokens even when nothing changed.

The acknowledgement is only an arm request, so confirm the active goal loop in
the dashboard. Stop it when the objective is met, the target becomes terminal,
or a person must decide. Normal approval rules still apply; an unattended action
may request approval, be rejected, or time out.
