"""Result aggregator — merges ExecutionResults into a single coherent response.

Takes a list of :class:`ExecutionResult` objects produced by the
:class:`ParallelExecutor` and combines them into an :class:`AggregatedResult`
with a unified text response, conflict detection, and OpenAI-compatible
output formatting.

Integration
-----------
* :class:`router.parallel_executor.ExecutionResult` – input results
* :class:`router.task_splitter.SubTask` – dependency metadata for ordering

Pipeline
--------
1. Extract text content from each result (OpenAI response format).
2. Order results by sub-task dependency graph (topological sort).
3. Concatenate with section headers.
4. Detect conflicts between results.
5. Format as an OpenAI-compatible response dict.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from router.parallel_executor import ExecutionResult
from router.task_splitter import SubTask

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AggregatedResult:
    """Combined result of merging multiple sub-task execution results.

    Attributes
    ----------
    combined_response:
        The merged text response from all sub-tasks.
    sub_results:
        Individual :class:`ExecutionResult` objects that were aggregated.
    success_count:
        Number of sub-tasks that completed successfully.
    failure_count:
        Number of sub-tasks that failed.
    conflicts:
        Descriptions of any detected conflicts between results.
    total_duration:
        Sum of all sub-task durations in seconds.
    metadata:
        Additional contextual information about the aggregation.
    """

    combined_response: str
    sub_results: List[ExecutionResult]
    success_count: int
    failure_count: int
    conflicts: List[str] = field(default_factory=list)
    total_duration: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ConflictDetector
# ---------------------------------------------------------------------------


# Regex to detect file paths in response text
_FILE_PATH_RE = re.compile(
    r"""(?:^|\s|["'`(])
    (
        (?:\.{0,2}/)?
        (?:[\w.-]+/)*
        [\w.-]+
        \.(?:py|js|ts|jsx|tsx|go|rs|java|c|cpp|h|hpp|cs|rb|php|swift|kt|scala
           |sh|bash|yaml|yml|json|toml|ini|cfg|conf|md|txt|html|css|scss|sql)
    )""",
    re.VERBOSE | re.IGNORECASE,
)

# Patterns that suggest modification actions on files (stem-based matching)
_MODIFY_KEYWORDS = re.compile(
    r"\b(?:modif\w*|chang\w*|updat\w*|edit\w*|rewrit\w*|replac\w*|delet\w*|"
    r"remov\w*|add\w*|creat\w*|renam\w*|mov\w*|refactor\w*|restructur\w*)",
    re.IGNORECASE,
)

# Patterns that suggest contradictory actions
_CONTRADICTION_PAIRS: List[Tuple[str, str]] = [
    (r"\bdelete\b", r"\b(?:keep|preserve|retain|add|create)\b"),
    (r"\bremove\b", r"\b(?:add|insert|create)\b"),
    (r"\brename\b", r"\bkeep the same name\b"),
    (r"\bsimplify\b", r"\badd more (?:complexity|features|logic)\b"),
    (r"\bmerge\b", r"\bsplit\b"),
    (r"\bdisable\b", r"\benable\b"),
    (r"\bdeprecate\b", r"\bintroduce\b"),
]


class ConflictDetector:
    """Detects conflicts between sub-task results.

    Performs simple heuristic-based conflict detection:

    * **File conflicts** — multiple sub-tasks modify the same file with
      potentially incompatible changes.
    * **Contradictory instructions** — results contain opposing directives
      (e.g. one says "delete" while another says "keep").
    """

    def detect(self, results: List[ExecutionResult]) -> List[str]:
        """Detect conflicts across a list of execution results.

        Parameters
        ----------
        results:
            Execution results from completed sub-tasks.

        Returns
        -------
        list[str]
            Human-readable conflict descriptions. Empty if no conflicts
            are detected.
        """
        conflicts: List[str] = []

        successful = [r for r in results if r.success and r.result is not None]
        if len(successful) < 2:
            return conflicts

        # --- File modification conflicts ---
        conflicts.extend(self._detect_file_conflicts(successful))

        # --- Contradictory instruction conflicts ---
        conflicts.extend(self._detect_contradictions(successful))

        return conflicts

    def _detect_file_conflicts(self, results: List[ExecutionResult]) -> List[str]:
        """Check if multiple results modify the same file differently."""
        conflicts: List[str] = []

        # Map: file path -> list of (sub_task_id, extracted content snippet)
        file_modifications: Dict[str, List[Tuple[int, str]]] = defaultdict(list)

        for r in results:
            content = self._extract_text(r)
            if not content:
                continue

            files_found = _FILE_PATH_RE.findall(content)
            has_modify_intent = bool(_MODIFY_KEYWORDS.search(content))

            if files_found and has_modify_intent:
                for fpath in set(files_found):
                    file_modifications[fpath].append((r.sub_task_id, content[:200]))

        for fpath, modifiers in file_modifications.items():
            if len(modifiers) >= 2:
                task_ids = [str(tid) for tid, _ in modifiers]
                conflicts.append(
                    f"File conflict: multiple sub-tasks ({', '.join(task_ids)}) "
                    f"attempt to modify '{fpath}' — manual review recommended."
                )

        return conflicts

    def _detect_contradictions(self, results: List[ExecutionResult]) -> List[str]:
        """Check for contradictory instructions across results."""
        conflicts: List[str] = []

        # Collect all content
        contents: List[Tuple[int, str]] = []
        for r in results:
            text = self._extract_text(r)
            if text:
                contents.append((r.sub_task_id, text.lower()))

        if len(contents) < 2:
            return conflicts

        # Check each contradiction pair across all content pairs
        for pattern_a, pattern_b in _CONTRADICTION_PAIRS:
            regex_a = re.compile(pattern_a, re.IGNORECASE)
            regex_b = re.compile(pattern_b, re.IGNORECASE)

            for i in range(len(contents)):
                for j in range(i + 1, len(contents)):
                    id_a, text_a = contents[i]
                    id_b, text_b = contents[j]

                    # Check both directions of the contradiction
                    if (regex_a.search(text_a) and regex_b.search(text_b)) or \
                       (regex_b.search(text_a) and regex_a.search(text_b)):
                        conflicts.append(
                            f"Contradiction: sub-task {id_a} and sub-task {id_b} "
                            f"contain potentially opposing instructions "
                            f"(matched '{pattern_a}' vs '{pattern_b}')."
                        )

        return conflicts

    @staticmethod
    def _extract_text(result: ExecutionResult) -> str:
        """Extract text content from an ExecutionResult."""
        if result.result is None:
            return ""
        try:
            choices = result.result.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                return message.get("content", "") or ""
        except (IndexError, KeyError, AttributeError):
            pass
        return ""


# ---------------------------------------------------------------------------
# ResultAggregator
# ---------------------------------------------------------------------------


class ResultAggregator:
    """Aggregates multiple :class:`ExecutionResult` objects into a unified response.

    Merging strategy:
      1. Extract text content from each result.
      2. Order results by sub-task dependency (topological sort).
      3. Concatenate with section headers.
      4. Detect conflicts between results.
      5. If all sub-tasks failed, return an error summary.

    Parameters
    ----------
    conflict_detector:
        Optional custom :class:`ConflictDetector`. A default instance is
        created if not provided.
    """

    def __init__(self, conflict_detector: Optional[ConflictDetector] = None) -> None:
        self.conflict_detector = conflict_detector or ConflictDetector()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def aggregate(
        self,
        results: List[ExecutionResult],
        sub_tasks: Optional[List[SubTask]] = None,
    ) -> AggregatedResult:
        """Aggregate execution results into a single coherent response.

        Parameters
        ----------
        results:
            List of :class:`ExecutionResult` from :class:`ParallelExecutor`.
        sub_tasks:
            Original :class:`SubTask` list, used for dependency ordering
            and section headers. If ``None``, results are ordered by
            ``sub_task_id``.

        Returns
        -------
        AggregatedResult
            Merged result with combined response, conflict info, and metadata.
        """
        if not results:
            return AggregatedResult(
                combined_response="",
                sub_results=[],
                success_count=0,
                failure_count=0,
                total_duration=0.0,
                metadata={"empty": True},
            )

        # Order results respecting dependency graph
        ordered_results = self._order_by_dependencies(results, sub_tasks)

        # Count successes and failures
        success_count = sum(1 for r in ordered_results if r.success)
        failure_count = len(ordered_results) - success_count

        # Total duration
        total_duration = sum(r.duration for r in ordered_results)

        # Detect conflicts
        conflicts = self.conflict_detector.detect(ordered_results)

        # Build combined response
        combined_response = self._build_combined_response(
            ordered_results, sub_tasks, success_count, failure_count,
        )

        # Build metadata
        metadata: Dict[str, Any] = {
            "total_sub_tasks": len(results),
            "success_count": success_count,
            "failure_count": failure_count,
            "conflict_count": len(conflicts),
            "total_duration": round(total_duration, 3),
            "ordering": [r.sub_task_id for r in ordered_results],
        }

        if sub_tasks:
            metadata["dependency_graph"] = {
                st.id: st.depends_on for st in sub_tasks
            }

        return AggregatedResult(
            combined_response=combined_response,
            sub_results=ordered_results,
            success_count=success_count,
            failure_count=failure_count,
            conflicts=conflicts,
            total_duration=total_duration,
            metadata=metadata,
        )

    def format_response(self, aggregated: AggregatedResult) -> Dict[str, Any]:
        """Format an AggregatedResult as an OpenAI-compatible response dict.

        The returned dict follows the ChatCompletion response schema with
        additional ``_session_manager`` metadata for the router layer.

        Parameters
        ----------
        aggregated:
            The aggregated result to format.

        Returns
        -------
        dict
            OpenAI-compatible response dictionary with ``id``, ``object``,
            ``choices``, ``usage``, and ``_session_manager`` fields.
        """
        # Estimate token usage from the combined response
        word_count = len(aggregated.combined_response.split())
        estimated_completion_tokens = int(word_count * 1.3)

        # Sum prompt tokens from sub-results if available
        total_prompt_tokens = 0
        total_completion_tokens = 0
        for r in aggregated.sub_results:
            if r.result and "usage" in r.result:
                usage = r.result["usage"]
                total_prompt_tokens += usage.get("prompt_tokens", 0)
                total_completion_tokens += usage.get("completion_tokens", 0)

        # Fall back to estimation if no usage data
        if total_completion_tokens == 0:
            total_completion_tokens = estimated_completion_tokens
        if total_prompt_tokens == 0:
            total_prompt_tokens = total_completion_tokens * 2  # rough estimate

        response: Dict[str, Any] = {
            "id": f"agg-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "aggregated",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": aggregated.combined_response,
                    },
                    "finish_reason": "stop" if aggregated.failure_count == 0 else "partial",
                }
            ],
            "usage": {
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
                "total_tokens": total_prompt_tokens + total_completion_tokens,
            },
            "_session_manager": {
                "aggregated": True,
                "sub_task_count": aggregated.metadata.get("total_sub_tasks", 0),
                "success_count": aggregated.success_count,
                "failure_count": aggregated.failure_count,
                "conflict_count": len(aggregated.conflicts),
                "total_duration": aggregated.total_duration,
                "ordering": aggregated.metadata.get("ordering", []),
                "sub_task_results": [
                    {
                        "sub_task_id": r.sub_task_id,
                        "success": r.success,
                        "duration": round(r.duration, 3),
                        "retries": r.retries,
                        "error": r.error,
                    }
                    for r in aggregated.sub_results
                ],
            },
        }

        # Include conflicts if any
        if aggregated.conflicts:
            response["_session_manager"]["conflicts"] = aggregated.conflicts

        # Include dependency graph if available
        if "dependency_graph" in aggregated.metadata:
            response["_session_manager"]["dependency_graph"] = \
                aggregated.metadata["dependency_graph"]

        return response

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_content(result: ExecutionResult) -> str:
        """Extract text content from an OpenAI-format response.

        Expects the result dict to follow the ChatCompletion schema::

            {"choices": [{"message": {"content": "..."}}]}

        Parameters
        ----------
        result:
            An execution result whose ``.result`` contains the API response.

        Returns
        -------
        str
            The extracted text content, or an empty string if extraction fails.
        """
        if result.result is None:
            return ""

        try:
            choices = result.result.get("choices", [])
            if not choices:
                return ""
            message = choices[0].get("message", {})
            content = message.get("content", "")
            return content if isinstance(content, str) else str(content)
        except (IndexError, KeyError, AttributeError, TypeError):
            return ""

    @staticmethod
    def _order_by_dependencies(
        results: List[ExecutionResult],
        sub_tasks: Optional[List[SubTask]],
    ) -> List[ExecutionResult]:
        """Order results respecting the dependency graph via topological sort.

        If ``sub_tasks`` is ``None`` or empty, results are sorted by
        ``sub_task_id`` in ascending order.

        Parameters
        ----------
        results:
            Unordered execution results.
        sub_tasks:
            Original sub-task definitions with dependency info.

        Returns
        -------
        list[ExecutionResult]
            Results in topological (dependency-respecting) order.
        """
        results_map: Dict[int, ExecutionResult] = {
            r.sub_task_id: r for r in results
        }

        if not sub_tasks:
            return sorted(results, key=lambda r: r.sub_task_id)

        # Build task lookup
        task_map: Dict[int, SubTask] = {st.id: st for st in sub_tasks}
        all_ids: Set[int] = set(task_map.keys())

        # Build in-degree counts (only for known dependencies)
        in_degree: Dict[int, int] = {}
        dependents: Dict[int, List[int]] = defaultdict(list)

        for st in sub_tasks:
            valid_deps = [d for d in st.depends_on if d in all_ids]
            in_degree[st.id] = len(valid_deps)
            for dep_id in valid_deps:
                dependents[dep_id].append(st.id)

        # Kahn's algorithm for topological sort
        ordered_ids: List[int] = []
        queue: List[int] = sorted(
            tid for tid, deg in in_degree.items() if deg == 0
        )

        while queue:
            current = queue.pop(0)
            ordered_ids.append(current)

            for dependent_id in sorted(dependents.get(current, [])):
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    queue.append(dependent_id)
            queue.sort()  # Keep deterministic ordering

        # Handle any remaining IDs (cycle — shouldn't happen, but be safe)
        remaining = set(results_map.keys()) - set(ordered_ids)
        ordered_ids.extend(sorted(remaining))

        # Return results in topological order, skipping any IDs without results
        return [results_map[tid] for tid in ordered_ids if tid in results_map]

    @staticmethod
    def _build_section_header(
        sub_task: SubTask,
        index: int,
        total: int,
    ) -> str:
        """Build a section header for a sub-task's response content.

        Parameters
        ----------
        sub_task:
            The sub-task being rendered.
        index:
            1-based position in the output sequence.
        total:
            Total number of sub-tasks in the response.

        Returns
        -------
        str
            A markdown-formatted section header string.
        """
        description = sub_task.description
        # Truncate long descriptions for the header
        if len(description) > 80:
            description = description[:77] + "..."
        return f"## Part {index}/{total}: {description}"

    def _build_combined_response(
        self,
        ordered_results: List[ExecutionResult],
        sub_tasks: Optional[List[SubTask]],
        success_count: int,
        failure_count: int,
    ) -> str:
        """Build the combined text response from ordered results.

        If all sub-tasks failed, returns an error summary instead of
        concatenated content.
        """
        # All failed → error summary
        if success_count == 0:
            return self._build_error_summary(ordered_results)

        # Build task lookup for headers
        task_map: Dict[int, SubTask] = {}
        if sub_tasks:
            task_map = {st.id: st for st in sub_tasks}

        total = len(ordered_results)
        sections: List[str] = []

        for idx, result in enumerate(ordered_results, start=1):
            # Build section header
            sub_task = task_map.get(result.sub_task_id)
            if sub_task:
                header = self._build_section_header(sub_task, idx, total)
            else:
                header = f"## Part {idx}/{total}: Sub-task {result.sub_task_id}"

            if result.success:
                content = self._extract_content(result)
                if content:
                    sections.append(f"{header}\n\n{content}")
                else:
                    sections.append(f"{header}\n\n_(No content returned)_")
            else:
                error_msg = result.error or "Unknown error"
                sections.append(
                    f"{header}\n\n"
                    f"⚠️ **Sub-task failed**: {error_msg} "
                    f"(retries: {result.retries}, duration: {result.duration:.2f}s)"
                )

        combined = "\n\n---\n\n".join(sections)

        # Append conflict warnings if any
        conflicts = self.conflict_detector.detect(ordered_results)
        if conflicts:
            conflict_section = "\n\n".join(f"- {c}" for c in conflicts)
            combined += f"\n\n---\n\n## ⚠️ Conflicts Detected\n\n{conflict_section}"

        return combined

    @staticmethod
    def _build_error_summary(results: List[ExecutionResult]) -> str:
        """Build an error summary when all sub-tasks have failed."""
        lines = [
            "## All sub-tasks failed\n",
            "None of the sub-tasks completed successfully. "
            "Below is a summary of errors encountered:\n",
        ]

        for r in sorted(results, key=lambda x: x.sub_task_id):
            error = r.error or "Unknown error"
            lines.append(
                f"- **Sub-task {r.sub_task_id}**: {error} "
                f"(retries: {r.retries}, duration: {r.duration:.2f}s)"
            )

        lines.append(
            f"\n**Total sub-tasks**: {len(results)} | "
            f"**Total duration**: {sum(r.duration for r in results):.2f}s"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM-based Result Aggregation (Phase 3 - Task 3.2)
# ---------------------------------------------------------------------------


class LLMResultAggregator:
    """Aggregates results using an LLM for intelligent merging.

    Uses an LLM via the BridgeAdapter to merge multiple ExecutionResult objects
    into a coherent, unified response. The LLM is prompted to:
    - Merge all results into one coherent response
    - Resolve any conflicts
    - Remove redundancy
    - Improve coherence and flow
    - Preserve important details from each sub-task

    Falls back to rule-based ResultAggregator if LLM fails.

    Parameters
    ----------
    adapter:
        Any object with an ``async send(request: dict, model: str)`` method.
    model:
        Model name to use for aggregation (default: 'deepseek-chat').
    """

    def __init__(self, adapter: Any, model: str = "deepseek-chat") -> None:
        self.adapter = adapter
        self.model = model
        self.fallback_aggregator = ResultAggregator()

    async def aggregate(
        self,
        results: List[ExecutionResult],
        sub_tasks: Optional[List[SubTask]] = None,
    ) -> AggregatedResult:
        """Aggregate execution results using LLM-based intelligent merging.

        Parameters
        ----------
        results:
            List of :class:`ExecutionResult` from :class:`ParallelExecutor`.
        sub_tasks:
            Original :class:`SubTask` list for context.

        Returns
        -------
        AggregatedResult
            Merged result with LLM-generated coherent response.
            Falls back to rule-based aggregation on LLM failure.
        """
        if not results:
            return AggregatedResult(
                combined_response="",
                sub_results=[],
                success_count=0,
                failure_count=0,
                total_duration=0.0,
                metadata={"empty": True, "aggregator": "llm"},
            )

        # Count successes and failures
        success_count = sum(1 for r in results if r.success)
        failure_count = len(results) - success_count
        total_duration = sum(r.duration for r in results)

        # If all failed, use rule-based error summary
        if success_count == 0:
            logger.info("LLMResultAggregator: all tasks failed, using rule-based error summary")
            fallback_result = self.fallback_aggregator.aggregate(results, sub_tasks)
            fallback_result.metadata["aggregator"] = "llm_fallback"
            return fallback_result

        # Extract content from successful results
        contents = []
        for idx, result in enumerate(results, start=1):
            if result.success:
                content = self._extract_content(result)
                if content:
                    # Add context about which sub-task this came from
                    sub_task_desc = "Unknown task"
                    if sub_tasks:
                        for st in sub_tasks:
                            if st.id == result.sub_task_id:
                                sub_task_desc = st.description
                                break
                    contents.append({
                        "sub_task_id": result.sub_task_id,
                        "description": sub_task_desc,
                        "content": content,
                    })

        if not contents:
            # No extractable content, fall back
            logger.warning("LLMResultAggregator: no extractable content, falling back")
            fallback_result = self.fallback_aggregator.aggregate(results, sub_tasks)
            fallback_result.metadata["aggregator"] = "llm_fallback"
            return fallback_result

        # Build the LLM prompt
        prompt = self._build_aggregation_prompt(contents, sub_tasks)

        try:
            # Call the LLM
            llm_response = await self._call_llm(prompt)

            # Parse the response
            combined_response = self._parse_llm_response(llm_response)

            # Detect conflicts using rule-based detector
            conflict_detector = ConflictDetector()
            conflicts = conflict_detector.detect(results)

            # Build metadata
            metadata: Dict[str, Any] = {
                "total_sub_tasks": len(results),
                "success_count": success_count,
                "failure_count": failure_count,
                "conflict_count": len(conflicts),
                "total_duration": round(total_duration, 3),
                "ordering": [r.sub_task_id for r in results],
                "aggregator": "llm",
            }

            if sub_tasks:
                metadata["dependency_graph"] = {
                    st.id: st.depends_on for st in sub_tasks
                }

            logger.info(
                "LLMResultAggregator: successfully merged %d results using LLM",
                len(contents),
            )

            return AggregatedResult(
                combined_response=combined_response,
                sub_results=results,
                success_count=success_count,
                failure_count=failure_count,
                conflicts=conflicts,
                total_duration=total_duration,
                metadata=metadata,
            )

        except Exception as e:
            logger.warning(
                "LLMResultAggregator: LLM call failed (%s), falling back to rule-based",
                e,
            )
            # Fall back to rule-based aggregation
            fallback_result = self.fallback_aggregator.aggregate(results, sub_tasks)
            fallback_result.metadata["aggregator"] = "llm_fallback"
            fallback_result.metadata["llm_error"] = str(e)
            return fallback_result

    def _build_aggregation_prompt(
        self,
        contents: List[Dict[str, Any]],
        sub_tasks: Optional[List[SubTask]],
    ) -> str:
        """Build the prompt for the LLM to merge results.

        Parameters
        ----------
        contents:
            List of dicts with 'sub_task_id', 'description', and 'content'.
        sub_tasks:
            Original sub-task definitions for additional context.

        Returns
        -------
        str
            The complete prompt for the LLM.
        """
        prompt = """You are a result aggregation expert. Your task is to merge multiple sub-task results into a single, coherent, unified response.

INSTRUCTIONS:
1. Merge all sub-task results into one coherent response
2. Resolve any conflicts between results
3. Remove redundancy while preserving important details
4. Improve coherence and flow between sections
5. Maintain technical accuracy
6. Use clear section headers when appropriate

SUB-TASK RESULTS:
"""
        for item in contents:
            prompt += f"\n### Sub-task {item['sub_task_id']}: {item['description']}\n"
            prompt += f"{item['content']}\n"

        if sub_tasks:
            prompt += "\nORIGINAL TASK CONTEXT:\n"
            if sub_tasks[0].original_context:
                prompt += f"{sub_tasks[0].original_context[:500]}\n"

        prompt += """
UNIFIED RESPONSE:
Provide a single, well-structured response that combines all the above sub-task results. Remove any redundant information, resolve conflicts, and ensure the response flows naturally as if it were generated from a single coherent task.
"""
        return prompt

    async def _call_llm(self, prompt: str) -> str:
        """Make an async call to the LLM via the adapter.

        Parameters
        ----------
        prompt:
            The aggregation prompt.

        Returns
        -------
        str
            The LLM's response content.

        Raises
        ------
        Exception
            If the LLM call fails.
        """
        request = {
            "messages": [
                {
                    "role": "system",
                    "content": "You are a result aggregation assistant. Produce coherent, unified responses.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 4000,
        }

        response = await self.adapter.send(request, self.model)

        # Extract content from response
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                content = message.get("content", "")
                if content:
                    return content

        raise ValueError("LLM response did not contain valid content")

    @staticmethod
    def _parse_llm_response(response: str) -> str:
        """Parse and clean the LLM's response.

        Parameters
        ----------
        response:
            Raw LLM response text.

        Returns
        -------
        str
            Cleaned response text.
        """
        # Strip leading/trailing whitespace
        cleaned = response.strip()

        # Remove any markdown code block wrappers if present
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)

        return cleaned

    @staticmethod
    def _extract_content(result: ExecutionResult) -> str:
        """Extract text content from an ExecutionResult.

        Parameters
        ----------
        result:
            An execution result.

        Returns
        -------
        str
            The extracted text content, or empty string if extraction fails.
        """
        if result.result is None:
            return ""

        try:
            choices = result.result.get("choices", [])
            if not choices:
                return ""
            message = choices[0].get("message", {})
            content = message.get("content", "")
            return content if isinstance(content, str) else str(content)
        except (IndexError, KeyError, AttributeError, TypeError):
            return ""


class HybridResultAggregator:
    """Combines rule-based and LLM-based aggregation with automatic strategy selection.

    Strategy:
    - If results count <= 2: use rule-based (faster, simpler)
    - If results count > 2: try LLM-based, fallback to rule-based
    - Can be forced to use one mode via the ``use_llm`` flag

    Parameters
    ----------
    adapter:
        Optional adapter for LLM calls. If None, always uses rule-based.
    model:
        Model name for LLM aggregation (default: 'deepseek-chat').
    use_llm:
        Optional flag to force mode:
        - True: always try LLM (if adapter provided)
        - False: always use rule-based
        - None: auto-select based on result count (default)
    """

    def __init__(
        self,
        adapter: Optional[Any] = None,
        model: str = "deepseek-chat",
        use_llm: Optional[bool] = None,
    ) -> None:
        self.rule_aggregator = ResultAggregator()
        self.llm_aggregator = (
            LLMResultAggregator(adapter, model) if adapter is not None else None
        )
        self.use_llm = use_llm

    async def aggregate(
        self,
        results: List[ExecutionResult],
        sub_tasks: Optional[List[SubTask]] = None,
    ) -> AggregatedResult:
        """Aggregate results using the appropriate strategy.

        Parameters
        ----------
        results:
            List of :class:`ExecutionResult` from :class:`ParallelExecutor`.
        sub_tasks:
            Original :class:`SubTask` list for context.

        Returns
        -------
        AggregatedResult
            Merged result from either rule-based or LLM-based aggregation.
        """
        if not results:
            return AggregatedResult(
                combined_response="",
                sub_results=[],
                success_count=0,
                failure_count=0,
                total_duration=0.0,
                metadata={"empty": True, "aggregator": "hybrid"},
            )

        # Determine strategy
        should_use_llm = self._should_use_llm(len(results))

        if should_use_llm and self.llm_aggregator is not None:
            logger.info(
                "HybridResultAggregator: using LLM for %d results",
                len(results),
            )
            result = await self.llm_aggregator.aggregate(results, sub_tasks)
            result.metadata["hybrid_strategy"] = "llm"
            return result
        else:
            logger.info(
                "HybridResultAggregator: using rule-based for %d results",
                len(results),
            )
            result = self.rule_aggregator.aggregate(results, sub_tasks)
            result.metadata["aggregator"] = "rule"
            result.metadata["hybrid_strategy"] = "rule"
            return result

    def _should_use_llm(self, result_count: int) -> bool:
        """Determine whether to use LLM based on result count and configuration.

        Parameters
        ----------
        result_count:
            Number of results to aggregate.

        Returns
        -------
        bool
            True if LLM should be used, False for rule-based.
        """
        # Forced mode
        if self.use_llm is True:
            return True
        if self.use_llm is False:
            return False

        # Auto mode: use LLM for > 2 results
        return result_count > 2


# ---------------------------------------------------------------------------
# Quick-test main
# ---------------------------------------------------------------------------


def main() -> None:
    """Smoke test: aggregate 5 mock results (3 success, 1 partial, 1 failure)."""

    # ---- Mock sub-tasks --------------------------------------------------

    sub_tasks = [
        SubTask(
            id=1,
            description="Fetch user profile data from API",
            depends_on=[],
            original_context="Build a user dashboard with profile and activity",
        ),
        SubTask(
            id=2,
            description="Fetch recent activity logs",
            depends_on=[],
            original_context="Build a user dashboard with profile and activity",
        ),
        SubTask(
            id=3,
            description="Analyze user engagement metrics",
            depends_on=[1],
            original_context="Build a user dashboard with profile and activity",
        ),
        SubTask(
            id=4,
            description="Generate dashboard HTML template",
            depends_on=[2, 3],
            original_context="Build a user dashboard with profile and activity",
        ),
        SubTask(
            id=5,
            description="Send notification email to admin",
            depends_on=[4],
            original_context="Build a user dashboard with profile and activity",
        ),
    ]

    # ---- Mock execution results ------------------------------------------

    results = [
        # Sub-task 1: SUCCESS — full content
        ExecutionResult(
            sub_task_id=1,
            success=True,
            result={
                "id": "mock-1",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": (
                            "User profile retrieved successfully.\n\n"
                            "- Name: Jane Doe\n"
                            "- Email: jane@example.com\n"
                            "- Role: Admin\n"
                            "- Last login: 2024-01-15T10:30:00Z\n\n"
                            "All profile fields are populated and validated."
                        ),
                    },
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 65,
                    "total_tokens": 185,
                },
            },
            error=None,
            duration=0.45,
            retries=0,
        ),
        # Sub-task 2: SUCCESS — full content
        ExecutionResult(
            sub_task_id=2,
            success=True,
            result={
                "id": "mock-2",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Activity logs retrieved (last 7 days):\n\n"
                            "1. Login — Jan 15, 10:30 AM\n"
                            "2. File upload — Jan 14, 3:15 PM\n"
                            "3. Settings update — Jan 13, 9:00 AM\n"
                            "4. Login — Jan 12, 8:45 AM\n"
                            "5. Logout — Jan 11, 6:00 PM\n\n"
                            "Total events: 47 in the past week."
                        ),
                    },
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 150,
                    "completion_tokens": 80,
                    "total_tokens": 230,
                },
            },
            error=None,
            duration=0.38,
            retries=0,
        ),
        # Sub-task 3: SUCCESS — content with file reference
        ExecutionResult(
            sub_task_id=3,
            success=True,
            result={
                "id": "mock-3",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Engagement analysis complete.\n\n"
                            "Key metrics:\n"
                            "- Weekly active days: 5/7\n"
                            "- Average session duration: 23 minutes\n"
                            "- Feature adoption rate: 78%\n\n"
                            "Updated metrics in dashboard/components/metrics.py "
                            "with new engagement scoring algorithm. The calculate_score() "
                            "function has been modified to weight recent activity more heavily."
                        ),
                    },
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 110,
                    "total_tokens": 310,
                },
            },
            error=None,
            duration=0.62,
            retries=1,
        ),
        # Sub-task 4: PARTIAL SUCCESS — content but with a warning note
        ExecutionResult(
            sub_task_id=4,
            success=True,
            result={
                "id": "mock-4",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Dashboard HTML generated.\n\n"
                            "Template structure:\n"
                            "- Header with user info\n"
                            "- Activity feed section\n"
                            "- Metrics visualization\n\n"
                            "Note: Updated dashboard/components/metrics.py to "
                            "add the render_dashboard() helper function."
                        ),
                    },
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 180,
                    "completion_tokens": 90,
                    "total_tokens": 270,
                },
            },
            error=None,
            duration=0.55,
            retries=0,
        ),
        # Sub-task 5: FAILURE — notification service down
        ExecutionResult(
            sub_task_id=5,
            success=False,
            result=None,
            error="ConnectionError: SMTP server at mail.example.com:587 refused connection",
            duration=3.21,
            retries=2,
        ),
    ]

    # ---- Run aggregation ------------------------------------------------

    aggregator = ResultAggregator()

    print("=" * 70)
    print("Result Aggregator — Smoke Test (5 results: 3 success, 1 partial, 1 fail)")
    print("=" * 70)

    # Aggregate
    aggregated = aggregator.aggregate(results, sub_tasks)

    # Report
    print(f"\nAggregation complete:")
    print(f"  Success: {aggregated.success_count}")
    print(f"  Failure: {aggregated.failure_count}")
    print(f"  Conflicts: {len(aggregated.conflicts)}")
    print(f"  Total duration: {aggregated.total_duration:.2f}s")
    print(f"  Ordering: {aggregated.metadata.get('ordering', [])}")

    # Print combined response
    print(f"\n{'─' * 70}")
    print("Combined Response:")
    print(f"{'─' * 70}")
    print(aggregated.combined_response)

    # Print conflicts
    if aggregated.conflicts:
        print(f"\n{'─' * 70}")
        print("Detected Conflicts:")
        print(f"{'─' * 70}")
        for i, conflict in enumerate(aggregated.conflicts, 1):
            print(f"  {i}. {conflict}")

    # Format as OpenAI response
    print(f"\n{'─' * 70}")
    print("Formatted OpenAI Response:")
    print(f"{'─' * 70}")
    formatted = aggregator.format_response(aggregated)

    print(f"  id: {formatted['id']}")
    print(f"  object: {formatted['object']}")
    print(f"  model: {formatted['model']}")
    print(f"  choices[0].finish_reason: {formatted['choices'][0]['finish_reason']}")
    print(f"  usage: {formatted['usage']}")
    print(f"  _session_manager.aggregated: {formatted['_session_manager']['aggregated']}")
    print(f"  _session_manager.sub_task_count: {formatted['_session_manager']['sub_task_count']}")
    print(f"  _session_manager.success_count: {formatted['_session_manager']['success_count']}")
    print(f"  _session_manager.failure_count: {formatted['_session_manager']['failure_count']}")
    print(f"  _session_manager.conflict_count: {formatted['_session_manager']['conflict_count']}")
    print(f"  _session_manager.ordering: {formatted['_session_manager']['ordering']}")

    # ---- Verification assertions ----------------------------------------

    print(f"\n{'─' * 70}")
    print("Verification:")
    print(f"{'─' * 70}")

    assert aggregated.success_count == 4, \
        f"Expected 4 successes, got {aggregated.success_count}"
    print(f"  ✓ success_count == 4")

    assert aggregated.failure_count == 1, \
        f"Expected 1 failure, got {aggregated.failure_count}"
    print(f"  ✓ failure_count == 1")

    assert aggregated.total_duration > 0, \
        f"Expected positive total_duration, got {aggregated.total_duration}"
    print(f"  ✓ total_duration > 0 ({aggregated.total_duration:.2f}s)")

    # Check ordering respects dependencies
    ordering = aggregated.metadata.get("ordering", [])
    assert ordering.index(1) < ordering.index(3), \
        "Task 1 should come before Task 3 (dependency)"
    assert ordering.index(2) < ordering.index(4), \
        "Task 2 should come before Task 4 (dependency)"
    assert ordering.index(3) < ordering.index(4), \
        "Task 3 should come before Task 4 (dependency)"
    assert ordering.index(4) < ordering.index(5), \
        "Task 4 should come before Task 5 (dependency)"
    print(f"  ✓ Dependency ordering respected")

    # Check conflicts detected (sub-tasks 3 and 4 both modify metrics.py)
    assert len(aggregated.conflicts) > 0, \
        "Expected at least one conflict (both tasks 3 and 4 modify metrics.py)"
    print(f"  ✓ File conflict detected ({len(aggregated.conflicts)} conflict(s))")

    # Check formatted response structure
    assert "id" in formatted, "Missing 'id' in formatted response"
    assert "choices" in formatted, "Missing 'choices' in formatted response"
    assert "usage" in formatted, "Missing 'usage' in formatted response"
    assert "_session_manager" in formatted, "Missing '_session_manager' metadata"
    print(f"  ✓ Formatted response has correct structure")

    # Check finish_reason is "partial" due to failure
    assert formatted["choices"][0]["finish_reason"] == "partial", \
        f"Expected 'partial' finish_reason, got {formatted['choices'][0]['finish_reason']}"
    print(f"  ✓ finish_reason == 'partial' (due to failure)")

    # ---- Test all-failed scenario ----------------------------------------

    print(f"\n{'─' * 70}")
    print("Testing all-failed scenario:")
    print(f"{'─' * 70}")

    all_failed = [
        ExecutionResult(sub_task_id=1, success=False, result=None,
                       error="Timeout", duration=5.0, retries=2),
        ExecutionResult(sub_task_id=2, success=False, result=None,
                       error="Connection refused", duration=3.0, retries=2),
    ]
    all_failed_agg = aggregator.aggregate(all_failed, sub_tasks[:2])
    assert "All sub-tasks failed" in all_failed_agg.combined_response
    assert all_failed_agg.success_count == 0
    assert all_failed_agg.failure_count == 2
    print(f"  ✓ All-failed scenario produces error summary")
    print(f"  ✓ combined_response starts with: {all_failed_agg.combined_response[:50]}...")

    # ---- Test empty results ----------------------------------------------

    print(f"\n{'─' * 70}")
    print("Testing empty results:")
    print(f"{'─' * 70}")

    empty_agg = aggregator.aggregate([], None)
    assert empty_agg.combined_response == ""
    assert empty_agg.success_count == 0
    assert empty_agg.metadata.get("empty") is True
    print(f"  ✓ Empty results handled correctly")

    # ---- Test HybridResultAggregator (rule-based mode) -------------------

    print(f"\n{'─' * 70}")
    print("Testing HybridResultAggregator (rule-based mode, no adapter):")
    print(f"{'─' * 70}")

    import asyncio as _asyncio

    async def _test_hybrid_rule_based():
        hybrid = HybridResultAggregator(adapter=None)

        # Test with 2 results (should use rule-based)
        two_results = results[:2]
        agg = await hybrid.aggregate(two_results, sub_tasks[:2])
        assert agg.metadata.get("hybrid_strategy") == "rule", \
            f"Expected 'rule' strategy, got {agg.metadata.get('hybrid_strategy')}"
        assert agg.success_count == 2
        print(f"  ✓ 2 results → rule-based strategy")

        # Test with 5 results but no adapter (should use rule-based)
        agg5 = await hybrid.aggregate(results, sub_tasks)
        assert agg5.metadata.get("hybrid_strategy") == "rule", \
            f"Expected 'rule' strategy (no adapter), got {agg5.metadata.get('hybrid_strategy')}"
        print(f"  ✓ 5 results with no adapter → rule-based strategy")

        # Test with use_llm=False (forced rule-based)
        hybrid_forced = HybridResultAggregator(adapter=None, use_llm=False)
        agg_forced = await hybrid_forced.aggregate(results, sub_tasks)
        assert agg_forced.metadata.get("hybrid_strategy") == "rule"
        print(f"  ✓ use_llm=False → forced rule-based")

        # Test empty results
        empty = await hybrid.aggregate([], None)
        assert empty.combined_response == ""
        assert empty.metadata.get("empty") is True
        print(f"  ✓ Hybrid empty results handled correctly")

    _asyncio.run(_test_hybrid_rule_based())

    # ---- Test LLMResultAggregator with mock adapter (fallback) -----------

    print(f"\n{'─' * 70}")
    print("Testing LLMResultAggregator with mock adapter:")
    print(f"{'─' * 70}")

    class MockLLMAdapter:
        """Mock adapter that returns a merged response."""

        def __init__(self, should_fail: bool = False):
            self.should_fail = should_fail
            self.call_count = 0

        async def send(self, request: Dict[str, Any], model: str) -> Dict[str, Any]:
            self.call_count += 1
            if self.should_fail:
                raise ConnectionError("Simulated LLM failure")
            return {
                "id": "mock-llm-agg",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": (
                            "## Unified Dashboard Report\n\n"
                            "The user dashboard has been successfully constructed "
                            "with the following components:\n\n"
                            "**User Profile**: Jane Doe (Admin) with full profile "
                            "data including email and last login timestamp.\n\n"
                            "**Activity Feed**: 47 events in the past week including "
                            "logins, file uploads, and settings updates.\n\n"
                            "**Engagement Metrics**: 5/7 weekly active days, "
                            "23-minute average session, 78% feature adoption.\n\n"
                            "The dashboard template integrates all components "
                            "with updated metrics calculations."
                        ),
                    },
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 500,
                    "completion_tokens": 120,
                    "total_tokens": 620,
                },
            }

    async def _test_llm_aggregator():
        # Test successful LLM aggregation
        mock_adapter = MockLLMAdapter(should_fail=False)
        llm_agg = LLMResultAggregator(adapter=mock_adapter, model="deepseek-chat")

        agg_result = await llm_agg.aggregate(results, sub_tasks)
        assert mock_adapter.call_count == 1, f"Expected 1 LLM call, got {mock_adapter.call_count}"
        assert agg_result.metadata.get("aggregator") == "llm"
        assert agg_result.success_count == 4
        assert agg_result.failure_count == 1
        assert "Unified Dashboard Report" in agg_result.combined_response
        print(f"  ✓ LLM aggregation successful (adapter called {mock_adapter.call_count} time)")
        print(f"  ✓ combined_response: {agg_result.combined_response[:80]}...")

        # Test format_response works with LLM-aggregated results
        formatted = aggregator.format_response(agg_result)
        assert "id" in formatted
        assert "choices" in formatted
        assert formatted["choices"][0]["message"]["content"] == agg_result.combined_response
        print(f"  ✓ format_response works with LLM-aggregated results")

        # Test LLM failure → fallback to rule-based
        fail_adapter = MockLLMAdapter(should_fail=True)
        llm_agg_fail = LLMResultAggregator(adapter=fail_adapter)

        agg_fail = await llm_agg_fail.aggregate(results, sub_tasks)
        assert agg_fail.metadata.get("aggregator") == "llm_fallback"
        assert "llm_error" in agg_fail.metadata
        assert agg_fail.success_count == 4
        assert agg_fail.failure_count == 1
        print(f"  ✓ LLM failure → rule-based fallback (error captured in metadata)")

        # Test empty results
        empty = await llm_agg.aggregate([], None)
        assert empty.combined_response == ""
        assert empty.metadata.get("empty") is True
        print(f"  ✓ LLM aggregator handles empty results")

        # Test all-failed scenario
        all_failed_results = [
            ExecutionResult(sub_task_id=1, success=False, result=None,
                           error="Timeout", duration=5.0, retries=2),
            ExecutionResult(sub_task_id=2, success=False, result=None,
                           error="Connection refused", duration=3.0, retries=2),
        ]
        all_fail_agg = await llm_agg.aggregate(all_failed_results, sub_tasks[:2])
        assert all_fail_agg.metadata.get("aggregator") == "llm_fallback"
        assert "All sub-tasks failed" in all_fail_agg.combined_response
        print(f"  ✓ All-failed scenario → rule-based error summary")

    _asyncio.run(_test_llm_aggregator())

    # ---- Test HybridResultAggregator with LLM ----------------------------

    print(f"\n{'─' * 70}")
    print("Testing HybridResultAggregator with LLM adapter:")
    print(f"{'─' * 70}")

    async def _test_hybrid_with_llm():
        mock_adapter = MockLLMAdapter(should_fail=False)
        hybrid = HybridResultAggregator(adapter=mock_adapter, model="deepseek-chat")

        # 2 results → rule-based (threshold is > 2)
        agg2 = await hybrid.aggregate(results[:2], sub_tasks[:2])
        assert agg2.metadata.get("hybrid_strategy") == "rule"
        assert mock_adapter.call_count == 0
        print(f"  ✓ 2 results → rule-based (no LLM call)")

        # 5 results → LLM (above threshold)
        agg5 = await hybrid.aggregate(results, sub_tasks)
        assert agg5.metadata.get("hybrid_strategy") == "llm"
        assert mock_adapter.call_count == 1
        print(f"  ✓ 5 results → LLM strategy (adapter called)")

        # Force LLM mode with 2 results
        hybrid_forced_llm = HybridResultAggregator(
            adapter=MockLLMAdapter(should_fail=False), use_llm=True
        )
        agg_forced = await hybrid_forced_llm.aggregate(results[:2], sub_tasks[:2])
        assert agg_forced.metadata.get("hybrid_strategy") == "llm"
        print(f"  ✓ use_llm=True → forced LLM even for 2 results")

        # Force rule-based mode with 5 results
        hybrid_forced_rule = HybridResultAggregator(
            adapter=MockLLMAdapter(should_fail=False), use_llm=False
        )
        agg_forced_rule = await hybrid_forced_rule.aggregate(results, sub_tasks)
        assert agg_forced_rule.metadata.get("hybrid_strategy") == "rule"
        print(f"  ✓ use_llm=False → forced rule-based even for 5 results")

        # LLM failure in hybrid → fallback
        hybrid_fail = HybridResultAggregator(
            adapter=MockLLMAdapter(should_fail=True), model="deepseek-chat"
        )
        agg_hybrid_fail = await hybrid_fail.aggregate(results, sub_tasks)
        # Should fall back to rule-based
        assert agg_hybrid_fail.success_count == 4
        print(f"  ✓ Hybrid LLM failure → graceful fallback to rule-based")

    _asyncio.run(_test_hybrid_with_llm())

    print(f"\n{'=' * 70}")
    print("ALL CHECKS PASSED ✓")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
