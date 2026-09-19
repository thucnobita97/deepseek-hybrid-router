"""Request analyzer - classifies incoming chat requests by type."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from router.models import ChatCompletionRequest, Message


class RequestType(str, Enum):
    TOOL_CALLING = "tool_calling"
    VISION = "vision"
    REASONING = "reasoning"
    SEARCH = "search"
    CHAT = "chat"


# Heuristic keyword sets (lowercased).
REASONING_KEYWORDS = [
    "explain why",
    "prove",
    "derive",
    "math",
    "calculate",
    "analyze",
    "architecture",
    "compare and contrast",
    "step by step reasoning",
    "think through",
    "design pattern",
    "theorem",
    "deduce",
    "formal proof",
]

SEARCH_KEYWORDS = [
    "latest",
    "current",
    "today",
    "recent",
    "news",
    "search for",
    "find out",
    "up to date",
    "what happened",
    "who won",
    "breaking",
    "yesterday",
    "this week",
]


@dataclass
class RequestAnalysis:
    request_type: RequestType
    confidence: float
    reasoning: str
    metadata: Dict[str, Any] = field(default_factory=dict)


def _extract_text(content: Optional[Any]) -> str:
    """Return the plain-text portion of a message's content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return " ".join(parts)
    return str(content)


class RequestAnalyzer:
    """Classifies a ChatCompletionRequest into a RequestType."""

    # Threshold for REASONING heuristic score.
    REASONING_THRESHOLD: int = 3

    def __init__(self, reasoning_threshold: Optional[int] = None):
        if reasoning_threshold is not None:
            self.REASONING_THRESHOLD = reasoning_threshold

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def analyze(self, request: ChatCompletionRequest) -> RequestAnalysis:
        """Run all detectors in priority order and return a RequestAnalysis."""
        # 1. Tool calling
        tool_result = self._detect_tool_calling(request)
        if tool_result:
            return tool_result

        # 2. Vision
        vision_result = self._detect_vision(request)
        if vision_result:
            return vision_result

        # 3. Reasoning
        reasoning_result = self._detect_reasoning(request)
        if reasoning_result:
            return reasoning_result

        # 4. Search
        search_result = self._detect_search(request)
        if search_result:
            return search_result

        # 5. Default: chat
        return RequestAnalysis(
            request_type=RequestType.CHAT,
            confidence=1.0,
            reasoning="No specialized signal detected; defaulting to general chat.",
            metadata={"message_count": len(request.messages)},
        )

    # Convenience class-level shortcut.
    @classmethod
    def classify(cls, request: ChatCompletionRequest) -> RequestAnalysis:
        return cls().analyze(request)

    # ------------------------------------------------------------------ #
    # Detectors
    # ------------------------------------------------------------------ #
    def _detect_tool_calling(self, request: ChatCompletionRequest) -> Optional[RequestAnalysis]:
        has_tools_def = bool(request.tools) or bool(request.functions)
        has_tool_role = any(m.role == "tool" for m in request.messages)
        has_tool_calls = any(getattr(m, "tool_calls", None) for m in request.messages)

        if has_tools_def or has_tool_role or has_tool_calls:
            reasons: List[str] = []
            if has_tools_def:
                reasons.append("request defines tools/functions")
            if has_tool_role:
                reasons.append("message with role='tool' present")
            if has_tool_calls:
                reasons.append("message contains tool_calls")
            return RequestAnalysis(
                request_type=RequestType.TOOL_CALLING,
                confidence=1.0,
                reasoning="Tool usage detected: " + "; ".join(reasons) + ".",
                metadata={
                    "has_tools_definition": has_tools_def,
                    "has_tool_role": has_tool_role,
                    "has_tool_calls": has_tool_calls,
                    "message_count": len(request.messages),
                },
            )
        return None

    def _detect_vision(self, request: ChatCompletionRequest) -> Optional[RequestAnalysis]:
        image_blocks: List[str] = []
        for msg in request.messages:
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict) and block.get("type") in ("image_url", "image"):
                        image_blocks.append(str(block.get("type")))

        if image_blocks:
            return RequestAnalysis(
                request_type=RequestType.VISION,
                confidence=1.0,
                reasoning=f"Found {len(image_blocks)} image block(s) in message content.",
                metadata={
                    "image_block_count": len(image_blocks),
                    "image_block_types": image_blocks,
                    "message_count": len(request.messages),
                },
            )
        return None

    def _detect_reasoning(self, request: ChatCompletionRequest) -> Optional[RequestAnalysis]:
        text = " ".join(_extract_text(m.content) for m in request.messages).lower()
        score = 0
        matched_keywords: List[str] = []
        for kw in REASONING_KEYWORDS:
            if kw in text:
                score += 1
                matched_keywords.append(kw)

        # Long user message bonus.
        user_texts = [_extract_text(m.content) for m in request.messages if m.role == "user"]
        longest_user = max((len(t) for t in user_texts), default=0)
        if longest_user > 500:
            score += 1

        # Multiple question marks bonus.
        question_marks = text.count("?")
        if question_marks >= 2:
            score += 1

        if score >= self.REASONING_THRESHOLD:
            confidence = min(1.0, 0.5 + 0.1 * score)
            return RequestAnalysis(
                request_type=RequestType.REASONING,
                confidence=round(confidence, 3),
                reasoning=(
                    f"Reasoning heuristic score={score} "
                    f"(threshold={self.REASONING_THRESHOLD})."
                ),
                metadata={
                    "score": score,
                    "threshold": self.REASONING_THRESHOLD,
                    "matched_keywords": matched_keywords,
                    "longest_user_message_chars": longest_user,
                    "question_mark_count": question_marks,
                    "message_count": len(request.messages),
                },
            )
        return None

    def _detect_search(self, request: ChatCompletionRequest) -> Optional[RequestAnalysis]:
        text = " ".join(_extract_text(m.content) for m in request.messages).lower()
        matched: List[str] = [kw for kw in SEARCH_KEYWORDS if re.search(rf"\b{re.escape(kw)}\b", text)]
        if matched:
            confidence = min(1.0, 0.5 + 0.15 * len(matched))
            return RequestAnalysis(
                request_type=RequestType.SEARCH,
                confidence=round(confidence, 3),
                reasoning=f"Search-intent keywords detected: {matched}.",
                metadata={
                    "matched_keywords": matched,
                    "message_count": len(request.messages),
                },
            )
        return None


__all__ = ["RequestAnalyzer", "RequestAnalysis", "RequestType"]
