"""The one base class for every deliberate refusal in this harness.

This package refuses rather than returning a number that would need a caveat to
interpret: an absent embedding model, a corpus whose checksum moved, a report path
that resolves somewhere protected, a cut-off no query could observe. Each refusal
raises so the reason travels with it.

Every one of those raises therefore needs a catch site, or the guard that exists to
print a good message dumps a traceback instead. That rule was applied per command by
hand for several rounds, and `bench fetch` was found missing it while `bench
retrieval` had it -- a tuple of exception types is a list to forget to update, which
is the same shape as the defect.

So the enforcement point is inheritance, not a tuple. The CLI catches
``BenchRefusal`` once at the dispatch boundary; a new refusal type is handled the
moment it inherits, and there is nowhere to forget to add it.

Deliberately dependency-free. The dispatch imports it before deciding what to do,
and this package's heavy modules (`vector_memory`, the llama.cpp loader) must stay
out of the boot path of every unrelated ``kirocrew`` subcommand.

Subclasses of ``RuntimeError`` as well, so existing ``except RuntimeError`` callers
and the four original exception names keep working unchanged.
"""

from __future__ import annotations


class BenchRefusal(RuntimeError):
    """A refusal this harness makes on purpose, carrying its own explanation.

    Catching this is catching "the benchmark declined to produce a number, and the
    message says why" -- which is a normal outcome to report, not a crash.
    """
