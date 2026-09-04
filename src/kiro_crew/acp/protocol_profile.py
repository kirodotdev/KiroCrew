"""Wire-protocol profile: the ACP dialect an adapter speaks, as data.

``AcpClient``/``AcpRuntime`` historically forked on ``_is_claude``-descended
branches for the handful of ways the public ACP wire differs from kiro-cli's:
the ``initialize`` ``protocolVersion`` (a date string for kiro, the integer ``1``
for public ACP), which ``PermissionOption`` field names WE emit/prefer
(``id``/``label`` vs ``optionId``/``name`` — the *parser* stays tolerant of both
regardless), whether ``session/set_config_option`` is used (e.g. to set the model
or an effort level), and whether the harness emits ``agent_thought_chunk``
reasoning updates.

Those are properties of the *harness*, not of the legacy backend string, so they
belong on the adapter. A frozen :class:`ProtocolProfile` names them in one place;
:data:`KIRO_PROFILE` carries today's kiro-cli bytes, :data:`STANDARD_ACP_PROFILE`
the public-ACP bytes, and :data:`KAS_PROFILE` the KAS relay's (kiro-cli's dialect
but the public-ACP integer ``protocolVersion``, verified against ``runtime.py``).
The adapter owns the profile (see :mod:`kiro_crew.acp.harness_adapters`); where
client/runtime code cannot reach an adapter object it derives the profile from
the backend string through the single :func:`profile_for_backend` mapping, so the
string→profile decision lives here and nowhere else.

This is a seam: every value equals the constant the corresponding ``_is_claude``
branch used, so kiro/kas/claude bytes are unchanged and the golden argv/handshake
tests pin no difference. The backend-identifier constants
(:data:`~kiro_crew.acp.harness_descriptor.ACP_BACKEND_KIRO` and its siblings) are
imported from the descriptor leaf rather than ``acp.types`` so this module has no
module-scope edge into the ``types`` -> ``harness_registry`` -> ``harness_adapters``
cycle; ``harness_adapters`` therefore imports the profile constants at module
scope like any ordinary dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union

from kiro_crew.acp.harness_descriptor import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
)

PermissionOptionStyle = Literal["kiro", "standard"]


@dataclass(frozen=True)
class ProtocolProfile:
    """The ACP wire dialect a harness speaks.

    Frozen because a profile is a fixed description of a harness's wire, shared
    by every session on that harness; nothing mutates it per session.

    - ``protocol_version``: the value sent as ``initialize.protocolVersion`` —
      kiro-cli's date string, or the public-ACP integer.
    - ``permission_option_style``: which ``PermissionOption`` field spelling WE
      emit/prefer. The parser reads both spellings regardless; this only decides
      what we produce. No production code consumes this yet — land its consumer
      with it, and until then do not gate parsing on it (the parser is
      deliberately harness-agnostic).
    - ``supports_set_config_option``: whether ``session/set_config_option`` is the
      channel for model / effort selection (vs kiro-cli's ``session/set_model``
      and ``set_mode``).
    - ``emits_thought_chunks``: whether the harness is OBSERVED to emit
      ``agent_thought_chunk`` reasoning updates as a dedicated update type. Not
      consumed in production today; ``False`` for kiro-cli records that we have
      not observed them, not a guarantee — ``_extract_text_chunk`` handles the
      update unconditionally, so do NOT gate chunk parsing on this flag or a
      harness's reasoning text would be silently dropped.
    """

    protocol_version: Union[str, int]
    permission_option_style: PermissionOptionStyle
    supports_set_config_option: bool
    emits_thought_chunks: bool


#: kiro-cli's wire: a date-string protocol version, kiro-style permission options,
#: no ``session/set_config_option`` (it uses ``session/set_model`` + ``set_mode``),
#: and no dedicated thought-chunk update type.
KIRO_PROFILE = ProtocolProfile(
    protocol_version="2025-08-22",
    permission_option_style="kiro",
    supports_set_config_option=False,
    emits_thought_chunks=False,
)

#: The KAS relay's wire: kiro-cli's dialect in every respect EXCEPT the
#: ``initialize`` ``protocolVersion``, which the relay expects as the public-ACP
#: integer ``1`` (``runtime.py``'s :data:`~kiro_crew.acp.runtime.PROTOCOL_VERSION_KAS`,
#: selected on the primary v3 path). Verified against the wire in ``runtime.py``,
#: not inferred from "the relay IS kiro-cli": treating KAS as :data:`KIRO_PROFILE`
#: would flip its handshake ``protocolVersion`` from ``1`` to the date string the
#: moment a client/runtime site reads the profile instead of forking on the
#: backend. The permission-option style, ``set_config_option`` posture, and
#: thought-chunk behaviour are kiro-cli's, since the relay IS a kiro-cli.
KAS_PROFILE = ProtocolProfile(
    protocol_version=1,
    permission_option_style="kiro",
    supports_set_config_option=False,
    emits_thought_chunks=False,
)

#: The public ACP wire (claude-agent-acp and the generic adapter): integer
#: protocol version ``1``, standard permission-option field names,
#: ``session/set_config_option`` for model/effort, and ``agent_thought_chunk``
#: reasoning updates.
STANDARD_ACP_PROFILE = ProtocolProfile(
    protocol_version=1,
    permission_option_style="standard",
    supports_set_config_option=True,
    emits_thought_chunks=True,
)


def profile_for_backend(backend: str) -> ProtocolProfile:
    """The :class:`ProtocolProfile` for a legacy ``acp_backend`` string.

    The single string→profile mapping, for client/runtime sites that hold a
    backend string rather than an adapter object. ``claude`` speaks the public
    ACP wire; the KAS relay speaks kiro-cli's dialect but with the public-ACP
    integer ``protocolVersion`` (:data:`KAS_PROFILE`); kiro-cli (``""``) and any
    unrecognized value speak kiro-cli's wire — kiro-cli's profile is the
    fail-safe default because it is the wire AcpClient's default path already
    speaks.
    """
    if backend == ACP_BACKEND_CLAUDE:  # harness-ok: this IS the string→profile map
        return STANDARD_ACP_PROFILE
    if backend == ACP_BACKEND_CODEX:  # harness-ok: this IS the string→profile map
        # codex-acp speaks the public ACP wire: integer protocolVersion 1 and
        # ``session/set_config_option`` for model/effort (upstream's
        # PROTOCOL_VERSION_CODEX and its membership in both
        # ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION and the effort set) — the
        # standard profile IS that wire. Mapped even while the seam is dormant,
        # so the day a provider registers it the handshake is already right.
        return STANDARD_ACP_PROFILE
    if backend == ACP_BACKEND_KAS:  # harness-ok: this IS the string→profile map
        return KAS_PROFILE
    # ACP_BACKEND_KIRO ("") and any unrecognized value → kiro-cli's wire, the
    # dialect AcpClient's default path already speaks.
    return KIRO_PROFILE
