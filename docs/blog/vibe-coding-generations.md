---
title: "Vibe coding 1.0 to 4.0, and the part the ladder leaves out"
author: zezhexu
created: 2026-08-11
---

# Vibe coding 1.0 to 4.0, and the part the ladder leaves out

These are the four phases I think vibe coding goes through.

- 1.0: you read the code in an IDE and chat with one agent.
- 2.0: there are too many sessions to hold in your head, and you have stopped
  reading the code. So the sessions go in a list and you switch between them.
  Kiro Crew's interface is here today.
- 3.0: you stop caring what any individual agent is doing. You give guidance and
  unblock ten or more agents at once.
- 4.0: you stop handing out tasks at all. An agent holds a standing mandate:
  maintain this service and handle its operations.

Every step is defined by an abstraction the person stops touching directly, which
is the same move as assembly to compiler to library to service. That is why I
think the ordering is not arbitrary and the endpoint is a role rather than a queue
of tasks.

What the ladder does not say is what has to exist underneath for any of those
steps to be survivable. That part is the actual work, and it is the rest of this
post.

## "Not caring" is a symptom, not a mechanism

Human attention is doing real work today. It is one of the mechanisms that keeps
output correct. Take it away and put nothing in its place and you have not
reached 3.0, you have reached unreviewed output at ten times the rate.

What actually gates each rung is duller than attention: the cost of a mistake
nobody catches, times how often it happens. That framing explains something the
attention story cannot. We already let formatters and dependency bots change code
with nobody reading the diff. Not because those tools are smart. Because when they
are wrong, very little happens. Blast radius appears nowhere on the ladder and it
is the thing holding the gate shut.

There is a second variable moving underneath, and it is economic. An agent-hour
is getting cheap relative to a human-hour. The sane response is to spend the
cheap thing to buy back the scarce one: run redundant agents, review
adversarially, make the system check its own work. That also gives a blunt way to
prioritize. Ask how many human-seconds a feature buys back per token it spends,
and a lot of arguments end quickly.

## Two axes wearing one label

1.0 to 2.0 was an interface reacting to throughput. The human handed over nothing
new. The switcher changed.

2.0 to 3.0 to 4.0 hands over responsibility itself: first decomposition, then
verification, eventually deciding what is worth doing.

Numbering all four on one line makes them look like even steps, and that invites
a specific mistake I would like us to skip. Build 3.0 as a better list, a grid, a
wall of live sessions, and you have re-run the 2.0 move at a higher N. It buys
time and hits the same wall later. A dashboard does not change the situation it
displays: a person is still watching agents and still deciding what each one does
next.

## What the human and the agents actually share

The generations are easier to tell apart by what sits between the human and the
agents than by how the human feels about it.

| | Shared artifact |
|---|---|
| 1.0 | Code. Text. |
| 2.0 | Code and memory. Text plus accumulated context. |
| 3.0 | A running environment and the verdict it produces. Behavior. |
| 4.0 | The organization itself: mandates, and who escalates to whom. |

Each step moves that shared thing closer to reality, which finally gives 3.0 a
definition I can act on. In 3.0 the human stops supplying the judgment of whether
the work is correct, and supplies a goal plus an oracle instead.

So the content of 3.0 is not a page. It is oracles. Tests that run in a real
environment, canaries, service-level checks, property tests, replay against
recorded production traffic, agents that check each other's work with a bias to
disprove. Every oracle you build buys back some number of agents you no longer
have to watch personally. It also makes progress countable: what fraction of the
changes agents produce get accepted or rejected by an oracle rather than by a
person?

Step back one more level and 3.0 and 4.0 are the same organ at different scopes.
3.0 builds the oracle that answers "is this change good". 4.0 builds the oracle
that answers "does something out there need attention right now". Both are the
same instruction: read reality instead of asking the human. It is also why
operations is the easiest place to start 4.0. Alarms, error budgets and pages are
already a machine-readable definition of "something is wrong", written by someone
else. Feature work has nothing like that, so I expect 4.0 to arrive one domain at
a time, in order of whether the domain hands you an oracle for free, and not as a
release.

## 3.0 is the rung that will hurt

Driving automation has the same shape and its level 3 is the one nobody enjoys.
The human is nominally out of the loop while remaining the fallback, and the
handoff is where people get hurt. Our 3.0 is that rung, which predicts two
failures that have nothing to do with how good the model is.

The first is rubber-stamping. At ten or more concurrent agents a human has
seconds per decision, so approving items one at a time turns into a reflex and
review becomes theater. The consequence is worth stating plainly: at that scale
the approval dialog is not what keeps you safe. Policy is. Deny rules that
actually block, limits on how much a given agent can touch, the sandbox, a
governance ceiling the agent cannot raise even when it wants to. That layer is
the real substrate of 3.0, and it is considerably less fun to demo than a wall of
live sessions.

The second is context-rebuild collapse. On the day something is genuinely wrong,
a human who has not looked at anything for a week needs twenty minutes to build
enough context to judge it, and throughput falls over exactly when it matters.
Which means "you no longer read the code" needs a qualifier: not by default, and
the cost of reading it when you must has to stay near zero. 1.0 is not a
superseded generation. It is 3.0's drill-down, and every escalation has to carry
a path back to the diff, the log and the session that caused it. The four rungs
are zoom levels on one control hierarchy, not versions that replace each other.

## Porting accountability

Here is the question I keep coming back to. Why does a person take responsibility
for what they did, and can that be moved into a machine?

Mechanically it is not a feeling. It is a loop with four parts. The action traces
back to somebody identifiable. There is a record of it that the actor cannot
edit. What the actor is allowed to do next depends on that record. And the chain
ends at a name that pays for it.

None of those four needs a conscience, which surprised me when I first wrote it
down. People are accountable mostly because of structure outside them. They can
be fired, sued, or embarrassed in front of colleagues. Their name is on the
commit. The team watched the outage happen. Accountability is far more
institutional than psychological, and that is good news for us, because you
cannot ship a conscience but you can ship an institution.

Kiro Crew already has the first part and the fourth, plus a narrow version of the
second: the security keystone is exactly "the agent may not read or write its own
ceiling". The third is missing entirely. Today an agent whose change gets
reverted suffers nothing at all, and nothing in its next turn mentions that it
happened.

### The payload is dynamic blast radius

This is where the accountability question and the gate turn out to be one
question. Permission should be a function of a verified track record.

A new agent may only touch tests. After twenty clean landings it may touch
production code. One regression it failed to declare puts it back to read-only
with mandatory human review. That sentence covers parts two and three of the
loop, it is entirely mechanical, and it assumes nothing whatsoever about the
agent's inner life.

It also does something to the rubber-stamp problem. Human attention stops going
into approving individual actions and goes into moving one agent between trust
tiers, which is one decision governing a hundred actions instead of a hundred
decisions governing one each.

As for the substitute for conscience: it is not simulated feeling, it is making
the record impossible to avoid at decision time. People do not feel guilt while
deciding. They remember what happened last time. An agent whose every turn opens
with "your last five changes: three landed, one reverted for X, one caused an
incident" has that memory supplied from outside. Nobody has to believe it has an
inner life for this to change its behavior.

## What does not port

Three limits, and I would rather be honest about them than sell a story.

Stake does not port. Nothing threatens a model. Deleting an agent is not a
deterrent to something with no interest in surviving, so an incentive economy
built on that premise is set dressing. The one hard mechanism in that family is a
cost cap, which works because it constrains the system rather than persuading the
agent.

Learning from consequence across episodes does not port either, not without
weight updates. The prosthetic we have, a durable record re-injected into the
next decision, is genuinely weaker than the thing it imitates.

Reputation needs a population that acts on your record, and we do not have one
before 4.0.

That second limit has a ceiling I can measure, and it is in this repository.
Episodic ranking multiplies similarity by `math.exp(-0.03 * days_old)`
(`src/kiro_crew/vector_memory.py:1562` and `:1651`, on `4506e9c92`), which is a
half-life of about 23 days. The score is then rounded to four decimals
(`src/kiro_crew/vector_memory.py:1565`), so once a memory is roughly a year old a
typical score underflows to `0.0000` and the sort order is gone with it.
Retrieval benchmarking with the harness in
[#2123](https://github.com/kirodotdev/KiroCrew/pull/2123) measures turn-level
recall far below session-level recall over a 293-day corpus.

Put those two facts next to each other and the result is uncomfortable. An
agent's old mistakes are forgotten by construction, right at the point where
long-horizon accountability would start to bite. So the record cannot ride on
semantic retrieval. It has to be a field that is always injected, not a memory
entry we hope gets recalled.

## Agent to agent is two layers, not one

I think coordination between agents is two problems that people keep merging, and
they do not arrive at the same time.

Resource arbitration is the first: who is editing this file, who owns this
subsystem right now. That is a 3.0 problem, it shows up the moment ten agents
share one repository, and it is mechanical.

Authority is the second: who may approve whom, who answers to whom when something
is ambiguous. That is 4.0, and it has a constraint that is easy to get wrong. An
agent hierarchy has to rest on asymmetric permission, not on an asymmetric
prompt. If agent B is agent A with a different system prompt, escalating to it
buys nothing but latency. Human escalation chains work because authority and
accountability are unequal, not because the person above you is smarter. Each
level has to hold strictly more of something real: permission, context, or a
verifier that is genuinely different, another model or another source of
evidence.

The other constraint came out of our own logs, and it is my favorite result so
far because it points the wrong way. In the first phase of the perpetual-agent
experiment, an agent escalated a decision correctly, respected the rule about not
asking the same question twice, and then waited politely forever, because the
delivery channel had quietly stopped working. Its side of the protocol was
perfect. It stalled because nothing in the system obliged the human to answer.
Over the same stretch the agent fabricated nothing while our own observing
tooling produced several false alarms. The unreliable half was us.

So accountability has to be written in both directions. An escalation carries a
deadline, and an escalation nobody answers has to reroute or narrow the mandate on
its own. Otherwise a standing mandate is not something the agent can actually
execute, no matter how well it behaves.

## Do not rebuild all of human society

When I said out loud that we need to rebuild human society for agents, I meant it,
but two things would make that a mistake.

The first is porting problems agents do not have. Human institutions carry
enormous machinery for scarcity, mortality, deception, self-interest and
factional politics. Today's agents have no self-interest to police. Copy
reputation markets and incentives and death into the system and you pay for all
that machinery without owning the problem it exists to solve. Build the mechanism
that matches a failure you have watched happen.

The second is subtler and I think it is the real risk: responsibility diffusion
is the one human failure that transfers cleanly. The more layers an organization
has, the harder it is to find who is accountable, and an agent hierarchy will
reproduce that instantly and faster than we can read the logs. It is a perfect
machine for "I did what upstream told me". So the first invariant of 4.0 is not an
incentive scheme. It is making diffusion impossible by construction: every action
traces to a named agent, every agent's mandate traces to a named human, and there
is no anonymous segment anywhere in between.

Worth saying without decoration: human society never solved this. It diluted
responsibility until the outcome was tolerable. Copy it wholesale and you inherit
that too, at machine speed.

## The smallest useful next step

The concrete gap right now is that our perpetual agent writes its own journal.
That is a self-report, not a record, and the second part of the loop breaks
exactly there.

What is missing is small, and it requires trusting the agent for none of it.
Every input already exists in the forge and in the gateway's own logs: per agent,
what happened to each pull request (landed, reverted, closed), what happened to
each issue, and how many human minutes it consumed. Write that into a file the
agent cannot modify, and inject it every turn without going through semantic
retrieval.

One artifact, and it delivers the second part of the loop, the input to the third,
and the denominator for the human-seconds-per-token question. It is also the
precondition for dynamic blast radius, because without a record you can trust, a
trust tier is just a vibe with extra steps.
