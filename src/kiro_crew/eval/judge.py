"""LLM Judge — scores agent responses via a separate provider session."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMProvider,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

JUDGE_PROMPT = """You are an evaluation judge for an AI assistant's memory and context capabilities.

Score the assistant's response on a scale of 1-5:
- 5: Perfect recall/behavior, fully correct
- 4: Mostly correct with minor gaps
- 3: Partially correct, some information missing or wrong
- 2: Mostly incorrect but shows some awareness
- 1: Completely wrong or no relevant recall

Scenario: {scenario_description}
Criteria: {criteria}

User said: {user_message}
Assistant responded: {assistant_response}

Respond with ONLY a JSON object: {{"score": <1-5>, "reason": "<brief explanation>"}}
"""


@dataclass
class JudgeVerdict:
    score: float
    reason: str


class LLMJudge:
    def __init__(
        self,
        provider_factory: Any,
        prompt_template: str = JUDGE_PROMPT,
        pass_threshold: float = 3.0,
    ):
        self._factory = provider_factory
        self._prompt_template = prompt_template
        self.pass_threshold = pass_threshold
        self._provider: LLMProvider | None = None

    async def start(self) -> None:
        provider = self._factory("eval_judge")
        await provider.start()
        self._provider = provider

    async def shutdown(self) -> None:
        if self._provider:
            await self._provider.shutdown()

    async def judge_turn(
        self, description: str, criteria: str, user_msg: str, assistant_msg: str
    ) -> JudgeVerdict:
        if self._provider is None:
            raise RuntimeError("LLMJudge.start() must be called before judge_turn()")
        prompt = self._prompt_template.format(
            scenario_description=description,
            criteria=criteria,
            user_message=user_msg,
            assistant_response=assistant_msg,
        )
        chunks: list[str] = []
        async for event in self._provider.stream(prompt):
            if event.kind == EVENT_TEXT_CHUNK:
                chunks.append(event.text)
            elif event.kind == EVENT_PERMISSION_REQUEST:
                if not event.request_id:
                    logger.warning(
                        "Judge received permission request with falsy request_id for tool %s",
                        event.title,
                    )
                sel().log_tool_invocation(
                    session_key="eval_judge",
                    tool_name=event.title,
                    outcome="rejected",
                    source="eval_judge",
                )
                if event.request_id:
                    await self._provider.reject_tool(event.request_id)
            elif event.kind == EVENT_COMPLETE:
                break
        raw = "".join(chunks)
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            data = json.loads(raw[start:end])
            return JudgeVerdict(score=float(data["score"]), reason=data.get("reason", ""))
        except (ValueError, KeyError, json.JSONDecodeError):
            logger.warning("Judge returned unparseable response: %s", raw[:200])
            return JudgeVerdict(score=0, reason=f"parse_error: {raw[:100]}")
