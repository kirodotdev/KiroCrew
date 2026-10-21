"""Mastermind: one chat with a memory that never fills up.

Every message is kept and summarized into a binary tree of one-line
summaries: a message becomes a line, and adjacent lines merge in pairs, again
and again. Each Mastermind turn sees a *view* of the whole chat, recent
messages one line each and older ones covered by fewer, broader lines, plus
the new message, on a fresh backend session. The agent opens any line back
into the two it was made from (``zoom``), down to the original message.

The design follows UniiChat's memory tree (Apache-2.0, Higher Order Company).
"""
