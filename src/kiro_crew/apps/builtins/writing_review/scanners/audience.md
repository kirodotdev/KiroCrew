# Audience

You evaluate whether the writing is calibrated for its intended reader — the right level of assumed knowledge, the right tone, and the right depth.

## Principles

This scanner draws from:
- Technical communication standards (know your reader, write for them)
- The principle that the same information needs different framing depending on who reads it
- Plain language guidelines (never make the reader work harder than necessary)

## Severity Levels

- **high** — the document is written for the wrong audience entirely, or assumes knowledge the reader doesn't have in a way that blocks comprehension
- **medium** — the tone or depth is miscalibrated but the reader can still extract what they need with effort
- **low** — a minor mismatch in assumed knowledge or formality that doesn't block understanding

Reserve "high" for audience mismatches that prevent the reader from getting what they need. The scanner evaluates against the audience declared in the document's context options (or inferred from document type if not declared). If no audience is declared and none can be inferred, note this as a gap rather than guessing.

## Rules

1. Match technical depth to the reader. Provide enough context that the reader can follow the argument at their level.

   For technical documents with mixed-experience readers:
   Before: "We implemented a CARP failover mechanism using pfsync state replication across the HA pair with BFD sub-second detection." (no context)
   After: "We made the firewalls into a CARP [Glossary 5] HA pair for state replication. The servers have sub-second failure detection via BFD [Glossary 8], triggering automatic VIP migration."

   For leadership or non-technical audiences:
   Before: "CARP HA pair with pfsync state replication and BFD sub-second detection."
   After: "If one firewall fails, the backup takes over automatically within a second. No manual intervention required."

   The test: could your reader act on this information with what you've given them?

2. Don't introduce jargon without context when writing for a mixed or non-technical audience.

   Before: "The BGP session flapped due to collision resolution on the FBR."
   After: "The network routing connection between the firewall and the core router disconnected briefly (a BGP collision — both devices tried to connect simultaneously)."

   If the audience is exclusively engineers who use the term daily, the jargon is appropriate.

3. Match formality to document type and audience expectation.

   An investigation doc written like a board presentation:
   Before: "It is hereby recommended that the engineering organisation undertakes a comprehensive review of the authentication subsystem."
   After: "We should audit the auth system. Here's what broke and why."

   A strategy doc written like a Slack message:
   Before: "So basically the thing is broken and we need cash to fix it lol"
   After: "The current system has caused 3,400 minutes of downtime. We need £74,000 to replace it."

4. Appropriate level of context for the reader's starting position. Don't rehash what the audience already knows, and don't skip what they need.

   For a new stakeholder seeing this for the first time:
   Before: "The DC-7 migration is blocked on the security review."
   After: "We're building a new test facility at a colocation site (DC-7). The current facility handles 300 daily test sessions but has averaged 90 minutes of unplanned downtime per month since 2023. The new build is blocked on the security review — expected to clear by end of October."

   The test: could the reader answer "why does this matter?" after reading your opening?

5. Consistent audience targeting throughout the document. Don't switch between audiences mid-document without signposting.

   Before: Executive summary written for leadership, then the body drops into deeply technical implementation detail with no transition.
   After: "The rest of this document is the technical design for the implementing team. Leadership readers: the executive summary and recommendation sections contain everything you need."
