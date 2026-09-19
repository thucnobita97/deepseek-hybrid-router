"""Context Preservation — maintains shared context between sub-tasks across sessions.

When a large request is split into sub-tasks that execute on different bridge
sessions, each sub-task needs the context produced by its predecessors.  This
module provides:

* :class:`ContextStore` — thread-safe in-memory key/value store for shared state.
* :class:`ContextExtractor` — extracts structured context (decisions, files,
  variables, summaries) from an :class:`ExecutionResult` using simple text
  analysis (no LLM in Phase 2).
* :class:`ContextInjector` — prepends a ``[CONTEXT FROM PREVIOUS TASKS]`` block
  to the user message of the next sub-task.
* :class:`ContextManager` — orchestrates extraction after each sub-task
  completes and injection before the next one starts.

Integration
-----------
* :class:`router.task_splitter.SubTask` — carries ``id``, ``description``,
  ``depends_on``, ``original_context``.
* :class:`router.parallel_executor.ExecutionResult` — carries ``sub_task_id``,
  ``success``, ``result`` (dict with OpenAI chat-completion shape), ``error``.

Concurrency
-----------
All mutable state in :class:`ContextStore` is protected by an
:class:`asyncio.Lock`, making the module safe for concurrent use inside
:class:`ParallelExecutor`'s topological-layer execution.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from router.task_splitter import SubTask
from router.parallel_executor import ExecutionResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONTEXT_HEADER = "[CONTEXT FROM PREVIOUS TASKS]"
_CONTEXT_FOOTER = "[END CONTEXT]"
_DEFAULT_MAX_SUMMARY_CHARS = 2000

# Patterns used by ContextExtractor for simple text analysis
_FILE_PATH_RE = re.compile(
    r"""
    (?:^|\s|["'`(])
    (
        (?:\.{0,2}/)?
        (?:[\w.-]+/)*
        [\w.-]+
        \.(?:py|js|ts|jsx|tsx|go|rs|java|c|cpp|h|hpp|cs|rb|php|swift|kt|scala
           |sh|bash|yaml|yml|json|toml|ini|cfg|conf|md|txt|html|css|scss|sql)
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

_DECISION_KEYWORDS = [
    "decided", "decision", "chose", "chosen", "selected", "will use",
    "approach", "strategy", "plan", "agreed", "recommend",
]

_VARIABLE_PATTERN = re.compile(
    r"""
    (?:^|\s)                    # word boundary
    (?:
        (?:const|let|var|def|val|final|static)\s+  # declaration keywords
        |
        (?:class|interface|type|enum)\s+            # type declarations
    )
    ([A-Za-z_]\w*)              # capture the identifier
    """,
    re.VERBOSE,
)

_CHANGE_INDICATORS = [
    "added", "removed", "changed", "updated", "modified", "created",
    "deleted", "renamed", "refactored", "fixed", "implemented",
]


# ---------------------------------------------------------------------------
# ContextStore
# ---------------------------------------------------------------------------


class ContextStore:
    """Thread-safe in-memory key/value store for shared sub-task context.

    All public methods are async and protected by an :class:`asyncio.Lock`
    so they can be called safely from concurrent coroutines.
    """

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def set(self, key: str, value: Any) -> None:
        """Store a piece of context under *key*."""
        async with self._lock:
            self._data[key] = value
            logger.debug("ContextStore.set: %s", key)

    async def get(self, key: str) -> Optional[Any]:
        """Retrieve context by *key*, or ``None`` if not present."""
        async with self._lock:
            return self._data.get(key)

    async def get_all(self) -> Dict[str, Any]:
        """Return a shallow copy of all stored context."""
        async with self._lock:
            return dict(self._data)

    async def merge(self, new_context: Dict[str, Any]) -> None:
        """Merge *new_context* into the store, overwriting existing keys."""
        async with self._lock:
            self._data.update(new_context)
            logger.debug("ContextStore.merge: %d keys", len(new_context))

    async def summarize(self, max_chars: int = _DEFAULT_MAX_SUMMARY_CHARS) -> str:
        """Produce a human-readable text summary of all stored context.

        The summary is truncated to *max_chars* characters.  Useful for
        injecting into a sub-task prompt where token budget matters.
        """
        async with self._lock:
            if not self._data:
                return ""

            lines: List[str] = []
            for key, value in self._data.items():
                if isinstance(value, list):
                    if value:
                        formatted = ", ".join(str(v) for v in value)
                        lines.append(f"- {key}: {formatted}")
                elif isinstance(value, dict):
                    if value:
                        formatted = "; ".join(f"{k}={v}" for k, v in value.items())
                        lines.append(f"- {key}: {formatted}")
                elif value is not None and value != "":
                    lines.append(f"- {key}: {value}")

            summary = "\n".join(lines)

            if len(summary) > max_chars:
                summary = summary[:max_chars - 3] + "..."

            return summary

    async def clear(self) -> None:
        """Remove all stored context."""
        async with self._lock:
            self._data.clear()
            logger.debug("ContextStore cleared")


# ---------------------------------------------------------------------------
# ContextExtractor
# ---------------------------------------------------------------------------


class ContextExtractor:
    """Extracts structured context from an :class:`ExecutionResult`.

    Uses simple text analysis (regex, keyword matching) — no LLM calls.
    This is a Phase 2 approach; a future phase may use a lightweight model
    for richer extraction.
    """

    def extract_context(self, result: ExecutionResult) -> Dict[str, Any]:
        """Extract important information from a sub-task execution result.

        Returns a dict with keys:
        - ``sub_task_id``: the originating sub-task ID
        - ``success``: whether the sub-task succeeded
        - ``files_modified``: list of file paths mentioned in the response
        - ``decisions``: list of decision-related sentences
        - ``variables``: list of identifiers declared in the response
        - ``summary``: a short text summary of the response content
        - ``changes``: list of change-indicator sentences
        """
        content = self._extract_content_text(result)
        if not content:
            return {
                "sub_task_id": result.sub_task_id,
                "success": result.success,
                "files_modified": [],
                "decisions": [],
                "variables": [],
                "summary": f"Sub-task {result.sub_task_id}: {'completed' if result.success else 'failed'}",
                "changes": [],
            }

        return {
            "sub_task_id": result.sub_task_id,
            "success": result.success,
            "files_modified": self._extract_files(content),
            "decisions": self._extract_decisions(content),
            "variables": self._extract_variables(content),
            "summary": self._make_summary(content, result.sub_task_id),
            "changes": self._extract_changes(content),
        }

    # -- internal helpers ---------------------------------------------------

    @staticmethod
    def _extract_content_text(result: ExecutionResult) -> str:
        """Pull the assistant response text from an ExecutionResult."""
        if not result.result:
            return ""

        # Standard OpenAI chat-completion shape:
        # {"choices": [{"message": {"content": "..."}}]}
        choices = result.result.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            content = message.get("content", "")
            if isinstance(content, str):
                return content

        # Fallback: try a direct "content" key
        content = result.result.get("content", "")
        if isinstance(content, str):
            return content

        return ""

    @staticmethod
    def _extract_files(text: str) -> List[str]:
        """Find file paths mentioned in the text."""
        matches = _FILE_PATH_RE.findall(text)
        # Deduplicate while preserving order
        seen: set = set()
        unique: List[str] = []
        for m in matches:
            if m not in seen:
                seen.add(m)
                unique.append(m)
        return unique

    @staticmethod
    def _extract_decisions(text: str) -> List[str]:
        """Find sentences containing decision-related keywords."""
        sentences = re.split(r'(?<=[.!?])\s+', text)
        decisions: List[str] = []
        for sentence in sentences:
            lower = sentence.lower()
            for keyword in _DECISION_KEYWORDS:
                if keyword in lower:
                    cleaned = sentence.strip()[:200]
                    if cleaned and cleaned not in decisions:
                        decisions.append(cleaned)
                    break
        return decisions[:10]  # cap at 10

    @staticmethod
    def _extract_variables(text: str) -> List[str]:
        """Find declared identifiers (variables, classes, functions)."""
        matches = _VARIABLE_PATTERN.findall(text)
        seen: set = set()
        unique: List[str] = []
        for m in matches:
            if m not in seen:
                seen.add(m)
                unique.append(m)
        return unique[:20]  # cap at 20

    @staticmethod
    def _extract_changes(text: str) -> List[str]:
        """Find sentences describing changes made."""
        sentences = re.split(r'(?<=[.!?])\s+', text)
        changes: List[str] = []
        for sentence in sentences:
            lower = sentence.lower()
            for indicator in _CHANGE_INDICATORS:
                if indicator in lower:
                    cleaned = sentence.strip()[:200]
                    if cleaned and cleaned not in changes:
                        changes.append(cleaned)
                    break
        return changes[:10]  # cap at 10

    @staticmethod
    def _make_summary(text: str, sub_task_id: int) -> str:
        """Create a short summary from the response text."""
        # Take first meaningful paragraph or first 300 chars
        paragraphs = text.strip().split("\n\n")
        if paragraphs:
            first = paragraphs[0].strip()
            if len(first) > 300:
                first = first[:297] + "..."
            return f"Sub-task {sub_task_id}: {first}"
        # Fallback
        snippet = text.strip()[:300]
        if len(text.strip()) > 300:
            snippet += "..."
        return f"Sub-task {sub_task_id}: {snippet}"


# ---------------------------------------------------------------------------
# ContextInjector
# ---------------------------------------------------------------------------


class ContextInjector:
    """Injects accumulated context into a sub-task's request dict.

    Prepends a ``[CONTEXT FROM PREVIOUS TASKS]`` block to the user message
    so the bridge session sees relevant prior work.
    """

    async def inject(
        self,
        sub_task: SubTask,
        context: ContextStore,
    ) -> Dict[str, Any]:
        """Build a request dict with context injected into the user message.

        Parameters
        ----------
        sub_task:
            The sub-task to prepare a request for.
        context:
            The shared :class:`ContextStore` with accumulated context.

        Returns
        -------
        dict
            An OpenAI-format request dict with ``messages`` key, where the
            user message has the context block prepended.
        """
        summary = await context.summarize()

        # Build the user message content
        if summary:
            user_content = (
                f"{_CONTEXT_HEADER}\n"
                f"{summary}\n"
                f"{_CONTEXT_FOOTER}\n"
                f"\n"
                f"{sub_task.description}"
            )
        else:
            user_content = sub_task.description

        # Build the full request
        messages: List[Dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are processing a sub-task as part of a larger request. "
                    "Focus only on the task described below."
                ),
            },
        ]

        # Include original context as a system hint when available
        if sub_task.original_context:
            messages.append({
                "role": "system",
                "content": f"Original request context:\n{sub_task.original_context[:1000]}",
            })

        messages.append({
            "role": "user",
            "content": user_content,
        })

        return {"messages": messages}


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------


class ContextManager:
    """Orchestrates context extraction and injection across sub-tasks.

    This is the main entry point for the context preservation system.
    After each sub-task completes, call :meth:`record_result` to extract
    and store its context.  Before dispatching the next sub-task, call
    :meth:`prepare_sub_task` to inject accumulated context.

    Example
    -------
    ::

        cm = ContextManager()

        # After sub-task 1 completes:
        await cm.record_result(result_1)

        # Before dispatching sub-task 2:
        request = await cm.prepare_sub_task(sub_task_2)
        response = await adapter.send(request, model)

        # After sub-task 2 completes:
        await cm.record_result(result_2)

        # ... and so on
    """

    def __init__(self) -> None:
        self._store = ContextStore()
        self._extractor = ContextExtractor()
        self._injector = ContextInjector()

    async def record_result(self, result: ExecutionResult) -> None:
        """Extract context from a completed sub-task and store it.

        Parameters
        ----------
        result:
            The :class:`ExecutionResult` from a completed sub-task.
        """
        extracted = self._extractor.extract_context(result)

        # Store under a sub-task-specific key namespace
        prefix = f"task_{result.sub_task_id}"
        context_to_store: Dict[str, Any] = {}

        # Store each piece of extracted context
        if extracted.get("summary"):
            context_to_store[f"{prefix}_summary"] = extracted["summary"]

        if extracted.get("files_modified"):
            context_to_store[f"{prefix}_files"] = extracted["files_modified"]

        if extracted.get("decisions"):
            context_to_store[f"{prefix}_decisions"] = extracted["decisions"]

        if extracted.get("variables"):
            context_to_store[f"{prefix}_variables"] = extracted["variables"]

        if extracted.get("changes"):
            context_to_store[f"{prefix}_changes"] = extracted["changes"]

        # Track overall success
        context_to_store[f"{prefix}_success"] = result.success

        # Track cumulative file list
        existing_files = await self._store.get("all_files_modified") or []
        new_files = extracted.get("files_modified", [])
        merged_files = list(dict.fromkeys(existing_files + new_files))  # dedupe, preserve order
        context_to_store["all_files_modified"] = merged_files

        await self._store.merge(context_to_store)

        logger.info(
            "Recorded context from sub-task %d: %d files, %d decisions, %d changes",
            result.sub_task_id,
            len(extracted.get("files_modified", [])),
            len(extracted.get("decisions", [])),
            len(extracted.get("changes", [])),
        )

    async def prepare_sub_task(self, sub_task: SubTask) -> Dict[str, Any]:
        """Inject accumulated context into a sub-task and return the request dict.

        Parameters
        ----------
        sub_task:
            The :class:`SubTask` to prepare.

        Returns
        -------
        dict
            An OpenAI-format request dict ready to send to the adapter.
        """
        request = await self._injector.inject(sub_task, self._store)

        has_context = _CONTEXT_HEADER in request["messages"][-1]["content"]
        logger.info(
            "Prepared sub-task %d: context %s",
            sub_task.id,
            "injected" if has_context else "empty (first task)",
        )

        return request

    async def get_shared_state(self) -> Dict[str, Any]:
        """Return a copy of the current shared context state."""
        return await self._store.get_all()

    async def clear(self) -> None:
        """Reset the context store, discarding all accumulated context."""
        await self._store.clear()
        logger.info("ContextManager cleared")


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


async def main() -> None:
    """Simulate 3 sub-tasks with context passing between them."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=" * 60)
    print("Context Manager — Smoke Test (3 sub-tasks)")
    print("=" * 60)

    cm = ContextManager()

    # --- Sub-task 1: Create a database module ---
    sub_task_1 = SubTask(
        id=1,
        description="Create the database connection module in db/connection.py",
        depends_on=[],
        original_context="Build a user management system with PostgreSQL",
    )

    print("\n--- Sub-task 1: No prior context ---")
    request_1 = await cm.prepare_sub_task(sub_task_1)
    user_msg_1 = request_1["messages"][-1]["content"]
    has_context_1 = _CONTEXT_HEADER in user_msg_1
    print(f"  Context injected: {has_context_1}")
    print(f"  User message preview: {user_msg_1[:100]}...")
    assert not has_context_1, "First task should have no prior context"

    # Simulate sub-task 1 result
    result_1 = ExecutionResult(
        sub_task_id=1,
        success=True,
        result={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        "Created the database connection module in db/connection.py.\n\n"
                        "Decided to use asyncpg for async PostgreSQL access. "
                        "The module defines a DatabasePool class with connect() and "
                        "disconnect() methods. Also added a get_connection() helper "
                        "function.\n\n"
                        "Files modified: db/connection.py, db/__init__.py\n\n"
                        "Added configuration via DATABASE_URL environment variable."
                    ),
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 80, "completion_tokens": 120, "total_tokens": 200},
        },
        duration=1.2,
    )

    await cm.record_result(result_1)
    state_1 = await cm.get_shared_state()
    print(f"  Context stored after task 1: {len(state_1)} keys")
    for k, v in state_1.items():
        val_str = str(v)[:80]
        print(f"    {k}: {val_str}")

    # --- Sub-task 2: Create user model ---
    sub_task_2 = SubTask(
        id=2,
        description="Create the User model in models/user.py",
        depends_on=[1],
        original_context="Build a user management system with PostgreSQL",
    )

    print("\n--- Sub-task 2: Context from task 1 ---")
    request_2 = await cm.prepare_sub_task(sub_task_2)
    user_msg_2 = request_2["messages"][-1]["content"]
    has_context_2 = _CONTEXT_HEADER in user_msg_2
    print(f"  Context injected: {has_context_2}")
    assert has_context_2, "Second task should have context from task 1"

    # Show the context block
    context_start = user_msg_2.index(_CONTEXT_HEADER)
    context_end = user_msg_2.index(_CONTEXT_FOOTER) + len(_CONTEXT_FOOTER)
    context_block = user_msg_2[context_start:context_end]
    print(f"  Context block:\n{context_block}")

    # Simulate sub-task 2 result
    result_2 = ExecutionResult(
        sub_task_id=2,
        success=True,
        result={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        "Created the User model in models/user.py using SQLAlchemy ORM.\n\n"
                        "The User class has fields: id, username, email, password_hash, "
                        "created_at. Chose to use bcrypt for password hashing.\n\n"
                        "Files modified: models/user.py, models/__init__.py\n\n"
                        "Added a create_user() factory function and validate_email() helper. "
                        "Removed the placeholder models/base.py that was no longer needed."
                    ),
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 150, "completion_tokens": 140, "total_tokens": 290},
        },
        duration=1.5,
    )

    await cm.record_result(result_2)
    state_2 = await cm.get_shared_state()
    print(f"\n  Context stored after task 2: {len(state_2)} keys")
    all_files = state_2.get("all_files_modified", [])
    print(f"  Cumulative files: {all_files}")
    assert "db/connection.py" in all_files
    assert "models/user.py" in all_files

    # --- Sub-task 3: Create API endpoints ---
    sub_task_3 = SubTask(
        id=3,
        description="Create REST API endpoints for user CRUD operations",
        depends_on=[1, 2],
        original_context="Build a user management system with PostgreSQL",
    )

    print("\n--- Sub-task 3: Context from tasks 1 and 2 ---")
    request_3 = await cm.prepare_sub_task(sub_task_3)
    user_msg_3 = request_3["messages"][-1]["content"]
    has_context_3 = _CONTEXT_HEADER in user_msg_3
    print(f"  Context injected: {has_context_3}")
    assert has_context_3, "Third task should have context from tasks 1 and 2"

    # Show full request structure
    print(f"  Messages count: {len(request_3['messages'])}")
    for i, msg in enumerate(request_3["messages"]):
        role = msg["role"]
        preview = msg["content"][:120].replace("\n", "\\n")
        print(f"    [{i}] {role}: {preview}...")

    # Simulate sub-task 3 result
    result_3 = ExecutionResult(
        sub_task_id=3,
        success=True,
        result={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        "Implemented REST API endpoints in api/users.py:\n"
                        "- GET /users — list all users\n"
                        "- GET /users/{id} — get user by ID\n"
                        "- POST /users — create new user\n"
                        "- PUT /users/{id} — update user\n"
                        "- DELETE /users/{id} — delete user\n\n"
                        "Decided to use FastAPI with the DatabasePool from db/connection.py "
                        "and the User model from models/user.py. Added request validation "
                        "with Pydantic schemas in api/schemas.py."
                    ),
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 200, "completion_tokens": 180, "total_tokens": 380},
        },
        duration=1.8,
    )

    await cm.record_result(result_3)

    # --- Final state ---
    print("\n--- Final shared state ---")
    final_state = await cm.get_shared_state()
    summary = await cm._store.summarize()
    print(f"  Total keys: {len(final_state)}")
    print(f"\n  Full summary ({len(summary)} chars):")
    for line in summary.split("\n"):
        print(f"    {line}")

    # --- Verify cumulative file tracking ---
    final_files = final_state.get("all_files_modified", [])
    print(f"\n  All files modified across tasks: {final_files}")
    assert len(final_files) >= 4, f"Expected >= 4 files, got {len(final_files)}"

    # --- Test clear ---
    await cm.clear()
    empty_state = await cm.get_shared_state()
    assert len(empty_state) == 0, "State should be empty after clear"
    print(f"\n  After clear(): {len(empty_state)} keys")

    # --- Test ContextExtractor edge cases ---
    print("\n--- Edge case: failed sub-task ---")
    failed_result = ExecutionResult(
        sub_task_id=99,
        success=False,
        result=None,
        error="Connection timeout",
        duration=30.0,
    )
    extracted = cm._extractor.extract_context(failed_result)
    print(f"  Extracted from failed result: {extracted}")
    assert extracted["success"] is False
    assert extracted["files_modified"] == []

    print(f"\n{'=' * 60}")
    print("ALL CHECKS PASSED ✓")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
