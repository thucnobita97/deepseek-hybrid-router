"""Task size detection and splitting for large requests.

Phase 1 (rule-based): Uses keyword matching, regex, and token estimation to
decide whether a request should be split and how.

Phase 3 (LLM-based): Uses an LLM via the BridgeAdapter (localhost:8000) to
produce smarter sub-task decompositions with dependency and priority metadata.
The HybridTaskSplitter orchestrates both approaches with automatic fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# BridgeAdapter endpoint – OpenAI-compatible /v1/chat/completions
_DEFAULT_BRIDGE_URL = "http://localhost:8000/v1/chat/completions"

# ---------------------------------------------------------------------------
# Token estimation: prefer tiktoken, fall back to word_count * 1.3
# ---------------------------------------------------------------------------
try:
    import tiktoken
    _encoder = tiktoken.get_encoding("cl100k_base")

    def _estimate_tokens(text: str) -> int:
        return len(_encoder.encode(text))
except ImportError:
    def _estimate_tokens(text: str) -> int:
        """Approximate token count: ~1.3 tokens per word for English text."""
        return int(len(text.split()) * 1.3)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TaskAnalysis:
    """Result of analyzing a request for split-worthiness."""
    needs_splitting: bool
    estimated_tokens: int
    complexity: str            # "simple" | "medium" | "complex"
    intent: str                # e.g. "refactor", "analyze", "generate", "translate", "general"
    suggested_split_count: int = 1


@dataclass
class SubTask:
    """A single sub-task produced by splitting a larger request.

    Attributes:
        id: Unique integer identifier for this sub-task
        description: Human-readable description of what to do
        depends_on: List of sub-task IDs that must complete before this one
        original_context: Full original request text (for reference)
        priority: Execution priority (1=highest, 10=lowest)
    """
    id: int
    description: str
    depends_on: List[int] = field(default_factory=list)
    original_context: str = ""
    priority: int = 5  # default priority


# ---------------------------------------------------------------------------
# Constants – keyword sets for detection heuristics
# ---------------------------------------------------------------------------

# Thresholds
TOKEN_SPLIT_THRESHOLD = 4000  # above this → recommend splitting

# Complexity keywords
_MULTI_FILE_KEYWORDS = [
    "all files", "every file", "across files", "multiple files",
    "entire codebase", "whole project", "every module", "all modules",
    "each module", "all classes", "every class",
]

_MULTI_STEP_KEYWORDS = [
    "step by step", "in order", "sequentially", "one by one",
    "first .* then", "after that", "next .* then", "finally",
]

_COMPLEXITY_PATTERNS: list[re.Pattern] = [
    re.compile(kw, re.IGNORECASE) for kw in _MULTI_FILE_KEYWORDS
] + [
    re.compile(kw, re.IGNORECASE) for kw in _MULTI_STEP_KEYWORDS
]

# Intent detection (ordered – first match wins)
_INTENT_MAP: list[tuple[str, list[str]]] = [
    ("refactor", ["refactor", "rewrite", "restructure", "reorganize", "clean up", "modernize"]),
    ("analyze", ["analyze", "review", "audit", "inspect", "explain", "diagnose", "find bugs"]),
    ("generate", ["generate", "create", "write", "build", "implement", "add", "develop"]),
    ("translate", ["translate", "convert", "port", "migrate", "transpile"]),
    ("test", ["test", "write tests", "unit test", "coverage", "mock"]),
    ("document", ["document", "add docs", "write docs", "javadoc", "docstring", "readme"]),
]

# File-path regex – matches Unix/Windows paths and relative paths with extensions
_FILE_PATH_RE = re.compile(
    r"""
    (?:^|\s|["'`(])                     # boundary before path
    (
        (?:\.{0,2}/)?                   # optional leading ./ or ../
        (?:[\w.-]+/)*                   # directory segments
        [\w.-]+                         # filename
        \.(?:py|js|ts|jsx|tsx|go|rs|java|c|cpp|h|hpp|cs|rb|php|swift|kt|scala
           |sh|bash|yaml|yml|json|toml|ini|cfg|conf|md|txt|html|css|scss|sql)
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Numbered-step patterns: "1.", "Step 1", "First,"
_NUMBERED_STEP_RE = re.compile(
    r"(?m)^\s*(?:\d+[.)]\s|step\s+\d+[.:]\s*|first[,:]\s|second[,:]\s|third[,:]\s|finally[,:]\s)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# TaskSizeDetector
# ---------------------------------------------------------------------------

class TaskSizeDetector:
    """Analyzes chat-completion requests to decide if splitting is beneficial."""

    def __init__(self, token_threshold: int = TOKEN_SPLIT_THRESHOLD):
        self.token_threshold = token_threshold

    # -- public API ---------------------------------------------------------

    def analyze(self, request) -> TaskAnalysis:
        """Analyze a ChatCompletionRequest (or any object with .messages).

        Returns a TaskAnalysis with splitting recommendation.
        """
        text = self._extract_text(request)
        tokens = _estimate_tokens(text)
        intent = self._detect_intent(text)
        complexity = self._detect_complexity(text, tokens)

        needs_split = self._should_split(text, tokens, complexity)
        split_count = self._suggest_split_count(text, tokens, complexity) if needs_split else 1

        return TaskAnalysis(
            needs_splitting=needs_split,
            estimated_tokens=tokens,
            complexity=complexity,
            intent=intent,
            suggested_split_count=split_count,
        )

    def estimate_tokens(self, text: str) -> int:
        """Public wrapper for token estimation."""
        return _estimate_tokens(text)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _extract_text(request) -> str:
        """Concatenate all message content from a request into one string."""
        parts: list[str] = []
        messages = getattr(request, "messages", None) or []
        for msg in messages:
            content = getattr(msg, "content", None)
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                # Multi-part content (e.g. vision messages)
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(part.get("text", ""))
        return "\n".join(parts)

    @staticmethod
    def _detect_intent(text: str) -> str:
        """Detect the user's primary intent from keywords."""
        lower = text.lower()
        for intent, keywords in _INTENT_MAP:
            for kw in keywords:
                if kw in lower:
                    return intent
        return "general"

    @staticmethod
    def _detect_complexity(text: str, tokens: int) -> str:
        """Classify complexity as simple / medium / complex."""
        lower = text.lower()
        pattern_hits = sum(1 for p in _COMPLEXITY_PATTERNS if p.search(lower))
        file_hits = len(_FILE_PATH_RE.findall(text))

        score = 0
        # Token contribution
        if tokens > 8000:
            score += 3
        elif tokens > 4000:
            score += 2
        elif tokens > 2000:
            score += 1

        # Pattern / file contribution
        score += pattern_hits
        if file_hits >= 3:
            score += 2
        elif file_hits >= 1:
            score += 1

        if score >= 4:
            return "complex"
        if score >= 2:
            return "medium"
        return "simple"

    def _should_split(self, text: str, tokens: int, complexity: str) -> bool:
        """Decide whether the task warrants splitting."""
        if tokens > self.token_threshold:
            return True
        if complexity == "complex":
            return True
        # Multi-file detection alone can trigger a split
        if len(_FILE_PATH_RE.findall(text)) >= 2:
            return True
        # Numbered steps
        if len(_NUMBERED_STEP_RE.findall(text)) >= 3:
            return True
        return False

    @staticmethod
    def _suggest_split_count(text: str, tokens: int, complexity: str) -> int:
        """Suggest how many sub-tasks to create."""
        # By files
        files = _FILE_PATH_RE.findall(text)
        if len(files) >= 2:
            return min(len(files), 10)

        # By numbered steps
        steps = _NUMBERED_STEP_RE.findall(text)
        if len(steps) >= 3:
            return min(len(steps), 10)

        # By token count – aim for ~2000 tokens per chunk
        if tokens > 0:
            return max(2, min(tokens // 2000, 10))

        # Fallback
        return 2


# ---------------------------------------------------------------------------
# TaskSplitter
# ---------------------------------------------------------------------------

class TaskSplitter:
    """Splits a large request into SubTasks using rule-based strategies.

    Strategy priority:
      1. Split by file  (when ≥ 2 distinct file paths are detected)
      2. Split by step  (when ≥ 3 numbered/sequential steps are detected)
      3. Split by token count (even distribution fallback)
    """

    def split(self, request, analysis: Optional[TaskAnalysis] = None) -> List[SubTask]:
        """Split a request into sub-tasks.

        Args:
            request: A ChatCompletionRequest (or similar object with .messages).
            analysis: Pre-computed TaskAnalysis; will compute if not given.

        Returns:
            A list of SubTask instances.
        """
        text = TaskSizeDetector._extract_text(request)

        if analysis is None:
            analysis = TaskSizeDetector().analyze(request)

        if not analysis.needs_splitting:
            # Nothing to split – return a single sub-task wrapping the whole request.
            return [
                SubTask(
                    id=1,
                    description=text[:500],
                    depends_on=[],
                    original_context=text,
                )
            ]

        # Strategy 1: split by file
        files = list(dict.fromkeys(_FILE_PATH_RE.findall(text)))  # dedupe, preserve order
        if len(files) >= 2:
            return self._split_by_file(text, files)

        # Strategy 2: split by step
        step_matches = list(_NUMBERED_STEP_RE.finditer(text))
        if len(step_matches) >= 3:
            return self._split_by_steps(text, step_matches)

        # Strategy 3: split by token count
        return self._split_by_tokens(text, analysis.suggested_split_count)

    # -- strategy implementations -------------------------------------------

    @staticmethod
    def _split_by_file(text: str, files: list[str]) -> List[SubTask]:
        """Create one sub-task per detected file path."""
        subtasks: list[SubTask] = []
        for idx, fpath in enumerate(files, start=1):
            # Extract a snippet of surrounding context (±100 chars around the file mention)
            match = re.search(re.escape(fpath), text)
            start = max(0, (match.start() if match else 0) - 100)
            end = min(len(text), (match.end() if match else 0) + 100)
            snippet = text[start:end].strip()

            subtasks.append(
                SubTask(
                    id=idx,
                    description=f"Process file: {fpath}\n\nContext: {snippet}",
                    depends_on=[],  # file-level tasks are independent by default
                    original_context=text,
                )
            )
        return subtasks

    @staticmethod
    def _split_by_steps(text: str, matches: list[re.Match]) -> List[SubTask]:
        """Create one sub-task per detected numbered step."""
        subtasks: list[SubTask] = []
        # Build step boundaries
        boundaries = [(m.start(), m.end()) for m in matches]

        for idx, (start, _end) in enumerate(boundaries):
            # Step text runs from this match to the next (or end of text)
            next_start = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(text)
            step_text = text[start:next_start].strip()

            subtasks.append(
                SubTask(
                    id=idx + 1,
                    description=f"Step {idx + 1}: {step_text[:300]}",
                    depends_on=[idx] if idx > 0 else [],  # sequential dependency
                    original_context=text,
                )
            )
        return subtasks

    @staticmethod
    def _split_by_tokens(text: str, num_splits: int) -> List[SubTask]:
        """Split text into roughly equal chunks by token count."""
        num_splits = max(1, num_splits)
        words = text.split()
        chunk_size = max(1, len(words) // num_splits)

        subtasks: list[SubTask] = []
        for idx in range(num_splits):
            start_word = idx * chunk_size
            end_word = start_word + chunk_size if idx < num_splits - 1 else len(words)
            chunk = " ".join(words[start_word:end_word])

            subtasks.append(
                SubTask(
                    id=idx + 1,
                    description=f"Part {idx + 1}/{num_splits}: {chunk[:300]}",
                    depends_on=[idx] if idx > 0 else [],  # sequential for context continuity
                    original_context=text,
                )
            )
        return subtasks


# ---------------------------------------------------------------------------
# LLM-based Task Splitting (Phase 3)
# ---------------------------------------------------------------------------

class LLMTaskSplitter:
    """Splits tasks using an LLM via the BridgeAdapter for intelligent decomposition.

    Uses the OpenAI-compatible /v1/chat/completions endpoint to analyze complex
    tasks and generate sub-tasks with proper dependencies and priorities.
    """

    def __init__(self, bridge_url: str = _DEFAULT_BRIDGE_URL, model: str = "deepseek-chat", timeout: float = 30.0):
        """Initialize the LLM-based splitter.

        Args:
            bridge_url: URL of the BridgeAdapter endpoint
            model: Model name to use for task decomposition
            timeout: Request timeout in seconds
        """
        self.bridge_url = bridge_url
        self.model = model
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def split(self, text: str, analysis: Optional[TaskAnalysis] = None) -> List[SubTask]:
        """Split a task into sub-tasks using LLM analysis.

        Args:
            text: The user's request text
            analysis: Optional pre-computed analysis (used for context)

        Returns:
            List of SubTask objects with dependencies and priorities

        Raises:
            ValueError: If LLM response is invalid or cannot be parsed
            httpx.RequestError: If the BridgeAdapter is unreachable
        """
        # Build the decomposition prompt
        prompt = self._build_prompt(text, analysis)

        # Call the LLM
        response = await self._call_llm(prompt)

        # Parse the JSON response into SubTask objects
        subtasks = self._parse_llm_response(response, text)

        logger.info(f"LLMTaskSplitter: decomposed task into {len(subtasks)} sub-tasks")
        return subtasks

    def _build_prompt(self, text: str, analysis: Optional[TaskAnalysis]) -> str:
        """Construct the prompt asking the LLM to decompose the task."""
        prompt = f"""You are a task decomposition expert. Analyze the following request and break it down into independent sub-tasks.

USER REQUEST:
{text}

"""
        if analysis:
            prompt += f"""CONTEXT:
- Detected complexity: {analysis.complexity}
- Detected intent: {analysis.intent}
- Estimated tokens: {analysis.estimated_tokens}

"""
        prompt += """INSTRUCTIONS:
1. Identify distinct, independent units of work
2. Determine dependencies between sub-tasks (which tasks must complete before others)
3. Assign priority (1=highest, 10=lowest) based on execution order and importance
4. Keep descriptions clear and actionable

Return a JSON object with this exact structure:
{
  "sub_tasks": [
    {
      "id": 1,
      "description": "Clear description of what to do",
      "depends_on": [],
      "priority": 1
    },
    {
      "id": 2,
      "description": "Another independent task",
      "depends_on": [1],
      "priority": 2
    }
  ]
}

JSON RESPONSE:"""
        return prompt

    async def _call_llm(self, prompt: str) -> str:
        """Make an async HTTP call to the BridgeAdapter."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a task decomposition assistant. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 2000,
        }

        response = await self._client.post(self.bridge_url, json=payload)
        response.raise_for_status()

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return content

    def _parse_llm_response(self, response: str, original_text: str) -> List[SubTask]:
        """Parse the LLM's JSON response into SubTask objects."""
        # Strip markdown code blocks if present
        cleaned = response.strip()
        if cleaned.startswith("```"):
            # Remove ```json or ``` and trailing ```
            lines = cleaned.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM response is not valid JSON: {e}\nResponse: {response}")

        if "sub_tasks" not in data or not isinstance(data["sub_tasks"], list):
            raise ValueError(f"LLM response missing 'sub_tasks' array: {data}")

        subtasks = []
        for item in data["sub_tasks"]:
            # Validate required fields
            if not all(k in item for k in ["id", "description"]):
                raise ValueError(f"Sub-task missing required fields (id, description): {item}")

            subtask = SubTask(
                id=int(item["id"]),
                description=str(item["description"]),
                depends_on=[int(d) for d in item.get("depends_on", [])],
                original_context=original_text,
                priority=int(item.get("priority", 5))
            )
            subtasks.append(subtask)

        # Validate dependencies reference valid IDs
        valid_ids = {st.id for st in subtasks}
        for st in subtasks:
            invalid_deps = set(st.depends_on) - valid_ids
            if invalid_deps:
                logger.warning(f"Sub-task {st.id} has invalid dependencies: {invalid_deps}")

        return subtasks

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()


class HybridTaskSplitter:
    """Orchestrates rule-based and LLM-based splitting with automatic fallback.

    Uses rule-based splitting for simple tasks (faster, no network overhead).
    Uses LLM-based splitting for complex tasks (smarter decomposition).
    Falls back to rule-based if LLM fails.
    """

    def __init__(self, bridge_url: str = _DEFAULT_BRIDGE_URL, model: str = "deepseek-chat"):
        """Initialize the hybrid splitter.

        Args:
            bridge_url: URL of the BridgeAdapter endpoint
            model: Model name to use for LLM-based decomposition
        """
        self.detector = TaskSizeDetector()
        self.rule_splitter = TaskSplitter()
        self.llm_splitter = LLMTaskSplitter(bridge_url=bridge_url, model=model)

    async def split(self, request, analysis: Optional[TaskAnalysis] = None) -> List[SubTask]:
        """Split a task using the appropriate strategy.

        Strategy selection:
        - Simple/medium complexity with <=3 sub-tasks: use rule-based
        - Complex or >3 sub-tasks: try LLM, fallback to rule-based

        Args:
            request: A ChatCompletionRequest or dict with 'messages'
            analysis: Optional pre-computed analysis

        Returns:
            List of SubTask objects
        """
        # Normalize request to dict
        if hasattr(request, "messages"):
            # ChatCompletionRequest object
            text = TaskSizeDetector._extract_text(request)
        elif isinstance(request, dict) and "messages" in request:
            # Plain dict
            text = self._extract_text_from_dict(request)
        else:
            # Assume it's already text
            text = str(request)

        # Compute analysis if needed
        if analysis is None:
            # Create a minimal request object for the detector
            class _MinimalRequest:
                def __init__(self, messages):
                    self.messages = messages

            messages = [{"role": "user", "content": text}]
            analysis = self.detector.analyze(_MinimalRequest(messages))

        # If no splitting needed, return single sub-task
        if not analysis.needs_splitting:
            return [
                SubTask(
                    id=1,
                    description=text[:500],
                    depends_on=[],
                    original_context=text,
                )
            ]

        # Strategy selection: use LLM for complex tasks
        use_llm = (
            analysis.complexity == "complex"
            or analysis.suggested_split_count > 3
        )

        if use_llm:
            try:
                logger.info(f"HybridTaskSplitter: using LLM for {analysis.complexity} task")
                return await self.llm_splitter.split(text, analysis)
            except Exception as e:
                logger.warning(f"LLM splitting failed, falling back to rule-based: {e}")
                # Fall through to rule-based

        # Use rule-based splitting
        logger.info(f"HybridTaskSplitter: using rule-based splitting")
        return self.rule_splitter.split(text, analysis)

    def _extract_text_from_dict(self, request: Dict[str, Any]) -> str:
        """Extract text from a dict with 'messages' key."""
        messages = request.get("messages", [])
        parts = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                # Multi-part content (vision messages)
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(part.get("text", ""))
        return "\n".join(parts)

    async def close(self):
        """Close the LLM splitter's HTTP client."""
        await self.llm_splitter.close()


# ---------------------------------------------------------------------------
# Quick-test main
# ---------------------------------------------------------------------------

def main():
    """Smoke-test the detector and splitter without external dependencies."""

    # Lightweight mock that quacks like ChatCompletionRequest
    class _Msg:
        def __init__(self, role: str, content: str):
            self.role = role
            self.content = content

    class _Req:
        def __init__(self, messages):
            self.messages = messages

    detector = TaskSizeDetector()
    splitter = TaskSplitter()

    # ---- Test 1: Simple request (no split) --------------------------------
    print("=" * 60)
    print("TEST 1 – Simple request")
    req1 = _Req([_Msg("user", "What is the capital of France?")])
    a1 = detector.analyze(req1)
    print(f"  tokens={a1.estimated_tokens}, complexity={a1.complexity}, "
          f"intent={a1.intent}, needs_split={a1.needs_splitting}")
    s1 = splitter.split(req1, a1)
    print(f"  sub-tasks: {len(s1)}")
    assert not a1.needs_splitting
    assert len(s1) == 1

    # ---- Test 2: Multi-file refactor (split by file) ----------------------
    print("=" * 60)
    print("TEST 2 – Multi-file refactor")
    req2 = _Req([_Msg("user", (
        "Refactor all files in the project. Update router/main.py, "
        "router/session.py, router/routing.py, and router/fallback.py "
        "to use async/await patterns consistently."
    ))])
    a2 = detector.analyze(req2)
    print(f"  tokens={a2.estimated_tokens}, complexity={a2.complexity}, "
          f"intent={a2.intent}, needs_split={a2.needs_splitting}, "
          f"suggested_splits={a2.suggested_split_count}")
    s2 = splitter.split(req2, a2)
    print(f"  sub-tasks: {len(s2)}")
    for st in s2:
        print(f"    [{st.id}] {st.description[:80]}...")
    assert a2.needs_splitting
    assert a2.intent == "refactor"
    assert len(s2) >= 2

    # ---- Test 3: Numbered steps -------------------------------------------
    print("=" * 60)
    print("TEST 3 – Numbered steps")
    req3 = _Req([_Msg("user", (
        "Please implement the following changes:\n"
        "1. Add authentication middleware to the API\n"
        "2. Create a user registration endpoint\n"
        "3. Add rate limiting to all routes\n"
        "4. Write unit tests for the new endpoints\n"
        "5. Update the README with new API documentation"
    ))])
    a3 = detector.analyze(req3)
    print(f"  tokens={a3.estimated_tokens}, complexity={a3.complexity}, "
          f"intent={a3.intent}, needs_split={a3.needs_splitting}, "
          f"suggested_splits={a3.suggested_split_count}")
    s3 = splitter.split(req3, a3)
    print(f"  sub-tasks: {len(s3)}")
    for st in s3:
        print(f"    [{st.id}] depends_on={st.depends_on} | {st.description[:60]}...")
    assert a3.needs_splitting
    assert len(s3) >= 3
    # Check sequential dependencies
    for st in s3[1:]:
        assert len(st.depends_on) > 0

    # ---- Test 4: Large text (token-based split) ---------------------------
    print("=" * 60)
    print("TEST 4 – Large text (token-based split)")
    large_text = ("Implement a comprehensive caching layer with Redis backend, "
                  "including TTL management, cache invalidation, and distributed locking. " * 200)
    req4 = _Req([_Msg("user", large_text)])
    a4 = detector.analyze(req4)
    print(f"  tokens={a4.estimated_tokens}, complexity={a4.complexity}, "
          f"intent={a4.intent}, needs_split={a4.needs_splitting}, "
          f"suggested_splits={a4.suggested_split_count}")
    s4 = splitter.split(req4, a4)
    print(f"  sub-tasks: {len(s4)}")
    assert a4.needs_splitting
    assert a4.suggested_split_count >= 2
    assert len(s4) == a4.suggested_split_count

    # ---- Test 5: LLMTaskSplitter._parse_llm_response (unit test) ----------
    print("=" * 60)
    print("TEST 5 – LLMTaskSplitter JSON parsing")
    llm_splitter = LLMTaskSplitter()
    
    # Test valid JSON response
    valid_response = """
    {
      "sub_tasks": [
        {"id": 1, "description": "Task A", "depends_on": [], "priority": 1},
        {"id": 2, "description": "Task B", "depends_on": [1], "priority": 2}
      ]
    }
    """
    subtasks = llm_splitter._parse_llm_response(valid_response, "original text")
    assert len(subtasks) == 2
    assert subtasks[0].id == 1
    assert subtasks[0].description == "Task A"
    assert subtasks[0].depends_on == []
    assert subtasks[0].priority == 1
    assert subtasks[1].depends_on == [1]
    print(f"  ✓ Parsed valid JSON: {len(subtasks)} sub-tasks")
    
    # Test JSON with markdown code blocks
    markdown_response = """```json
    {
      "sub_tasks": [
        {"id": 1, "description": "First task", "depends_on": []}
      ]
    }
    ```"""
    subtasks2 = llm_splitter._parse_llm_response(markdown_response, "original")
    assert len(subtasks2) == 1
    assert subtasks2[0].id == 1
    print(f"  ✓ Parsed markdown-wrapped JSON: {len(subtasks2)} sub-task")
    
    # Test invalid JSON (should raise ValueError)
    try:
        llm_splitter._parse_llm_response("not valid json", "original")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "not valid JSON" in str(e)
        print(f"  ✓ Correctly rejected invalid JSON")
    
    # Test missing sub_tasks key
    try:
        llm_splitter._parse_llm_response('{"wrong_key": []}', "original")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "sub_tasks" in str(e)
        print(f"  ✓ Correctly rejected response without 'sub_tasks'")

    # ---- Test 6: HybridTaskSplitter with fallback -------------------------
    print("=" * 60)
    print("TEST 6 – HybridTaskSplitter (rule-based fallback)")
    hybrid = HybridTaskSplitter()
    
    # Simple task - should use rule-based
    simple_req = _Req([_Msg("user", "What is Python?")])
    simple_analysis = detector.analyze(simple_req)
    print(f"  Simple task: complexity={simple_analysis.complexity}, needs_split={simple_analysis.needs_splitting}")
    
    # We can't actually call the async split() without an event loop in this sync test,
    # but we can verify the splitter was created correctly
    assert hybrid.detector is not None
    assert hybrid.rule_splitter is not None
    assert hybrid.llm_splitter is not None
    print(f"  ✓ HybridTaskSplitter initialized with all components")
    
    # Test dict-based request extraction
    dict_req = {"messages": [{"role": "user", "content": "Test message"}]}
    text = hybrid._extract_text_from_dict(dict_req)
    assert text == "Test message"
    print(f"  ✓ Dict-based request extraction works")

    print("=" * 60)
    print("ALL TESTS PASSED ✓")
    print("\nNote: Full async HybridTaskSplitter.split() tests require an active")
    print("BridgeAdapter at localhost:8000 and should be run in an async test suite.")


if __name__ == "__main__":
    main()
