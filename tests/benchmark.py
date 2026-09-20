#!/usr/bin/env python3
"""
Provider Benchmark Suite — DeepSeek Bridge vs Paid APIs (DeepInfra, Alibaba)

Benchmarks LLM providers directly (bypasses the router) across 5 categories:
  1. Short chat        — quick single-turn Q&A
  2. Long chat         — ~500-word prompt to test large-context handling
  3. Reasoning         — math/logic problems
  4. Code generation   — function/class generation tasks
  5. Tool calling      — structured function-calling accuracy

Metrics collected per iteration:
  - TTFB (time to first byte, streaming mode)
  - Total time (wall-clock request duration)
  - Success/failure
  - Prompt tokens, completion tokens, total tokens
  - Quality heuristics (response length, tool-call validity, etc.)

Usage
-----
    # Run all providers, 3 iterations each
    python tests/benchmark.py

    # Only DeepSeek bridge, 5 iterations
    python tests/benchmark.py --providers deepseek-bridge --iterations 5

    # DeepInfra + Alibaba, save to results.json
    python tests/benchmark.py --providers deepinfra alibaba --output results.json

    # List available providers
    python tests/benchmark.py --list
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader — no extra deps needed."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value

# Load from project root
_project_root = Path(__file__).resolve().parent.parent
_load_dotenv(str(_project_root / ".env"))


# ---------------------------------------------------------------------------
# Provider configurations
# ---------------------------------------------------------------------------

@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    models: List[str]
    requires_auth: bool = True
    timeout: float = 120.0
    connect_timeout: float = 15.0

    def get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.requires_auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


PROVIDERS: Dict[str, ProviderConfig] = {
    "deepseek-bridge": ProviderConfig(
        name="deepseek-bridge",
        base_url="http://localhost:8002/v1/chat/completions",
        api_key="not-needed",
        models=["deepseek-chat"],
        requires_auth=False,
    ),
    "deepinfra": ProviderConfig(
        name="deepinfra",
        base_url="https://api.deepinfra.com/v1/chat/completions",
        api_key=os.getenv("DEEPINFRA_API_KEY", ""),
        models=[
            "deepseek-ai/DeepSeek-V4-Flash-0731",
            "zai-org/GLM-5.3-Flash",
        ],
    ),
    "alibaba": ProviderConfig(
        name="alibaba",
        base_url="https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
        api_key=os.getenv("ALIBABA_API_KEY", ""),
        models=[
            "qwen3.7-max",
            "qwen3.8-flash",
        ],
    ),
}


# ---------------------------------------------------------------------------
# Test case definitions
# ---------------------------------------------------------------------------

@dataclass
class TestCase:
    category: str
    name: str
    messages: List[Dict[str, str]]
    max_tokens: int = 1024
    tools: Optional[List[Dict]] = None
    quality_checks: Optional[List[str]] = None  # keywords to look for in response


# -- Short chat ---------------------------------------------------------------

SHORT_CHAT_CASES = [
    TestCase(
        category="short_chat",
        name="greeting",
        messages=[
            {"role": "user", "content": "What is the capital of France? Answer in one sentence."},
        ],
        max_tokens=100,
        quality_checks=["Paris"],
    ),
    TestCase(
        category="short_chat",
        name="factual",
        messages=[
            {"role": "user", "content": "What year was the Internet invented? Give a brief answer."},
        ],
        max_tokens=150,
        quality_checks=["1969", "1983", "ARPANET", "TCP/IP"],
    ),
    TestCase(
        category="short_chat",
        name="translation",
        messages=[
            {"role": "user", "content": "Translate 'Hello, how are you?' to Japanese, Chinese, and Spanish."},
        ],
        max_tokens=200,
    ),
]


# -- Long chat (~500 words prompt) -------------------------------------------

_LONG_PROMPT = """You are an expert historian. Write a detailed essay about the history of computing \
from the 1940s to the present day. Cover the following topics in at least 2 paragraphs each:

1. The invention of the first electronic computers (ENIAC, Colossus) and their role in World War II.
2. The development of transistors and integrated circuits in the 1950s-60s.
3. The rise of personal computing in the 1970s-80s (Apple, IBM PC, Microsoft).
4. The birth of the Internet (ARPANET, TCP/IP, World Wide Web) and its impact on society.
5. The mobile revolution (iPhone, Android) and the shift to cloud computing.
6. The era of artificial intelligence and machine learning — from expert systems to deep learning \
to large language models like GPT and DeepSeek.
7. The current state of quantum computing and its potential.
8. Ethical considerations: privacy, algorithmic bias, job displacement, and AI safety.

Please provide a comprehensive, well-structured essay with clear transitions between sections. \
Aim for approximately 800-1000 words in your response."""

LONG_CHAT_CASES = [
    TestCase(
        category="long_chat",
        name="essay_500word_prompt",
        messages=[
            {"role": "system", "content": "You are a knowledgeable historian and technology writer."},
            {"role": "user", "content": _LONG_PROMPT},
        ],
        max_tokens=2048,
        quality_checks=["ENIAC", "transistor", "Internet", "AI", "quantum"],
    ),
    TestCase(
        category="long_chat",
        name="technical_deep_dive",
        messages=[
            {"role": "user", "content": (
                "Explain in detail how transformer neural networks work, covering: "
                "self-attention mechanism, multi-head attention, positional encoding, "
                "layer normalization, feed-forward layers, and the training process "
                "(pre-training, fine-tuning, RLHF). Provide code-level pseudocode for "
                "the attention computation. Target ~800 words."
            )},
        ],
        max_tokens=2048,
        quality_checks=["attention", "encoder", "decoder", "softmax"],
    ),
]


# -- Reasoning (math/logic) ---------------------------------------------------

REASONING_CASES = [
    TestCase(
        category="reasoning",
        name="math_arithmetic",
        messages=[
            {"role": "user", "content": (
                "Solve step by step: A train leaves station A at 60 km/h. "
                "Another train leaves station B (300 km away) at 90 km/h toward A. "
                "A bird flies at 120 km/h between them, starting from A. "
                "How far does the bird travel before the trains meet?"
            )},
        ],
        max_tokens=500,
        quality_checks=["240", "2 hours"],
    ),
    TestCase(
        category="reasoning",
        name="logic_puzzle",
        messages=[
            {"role": "user", "content": (
                "Three friends — Alice, Bob, and Charlie — each have a different pet "
                "(cat, dog, fish) and a different favorite color (red, blue, green). "
                "Clues:\n"
                "1. Alice doesn't like red.\n"
                "2. The person with the cat likes blue.\n"
                "3. Bob has the dog.\n"
                "4. Charlie likes green.\n"
                "Who has which pet and which color? Explain your reasoning step by step."
            )},
        ],
        max_tokens=500,
        quality_checks=["Alice", "Bob", "Charlie"],
    ),
    TestCase(
        category="reasoning",
        name="probability",
        messages=[
            {"role": "user", "content": (
                "You have a bag with 3 red balls and 2 blue balls. You draw 2 balls "
                "without replacement. What is the probability that both are the same color? "
                "Show all work."
            )},
        ],
        max_tokens=400,
        quality_checks=["0.4", "2/5", "40%"],
    ),
]


# -- Code generation ----------------------------------------------------------

CODE_GEN_CASES = [
    TestCase(
        category="code_gen",
        name="binary_search",
        messages=[
            {"role": "user", "content": (
                "Write a Python function `binary_search(arr, target)` that performs "
                "binary search on a sorted list. Include type hints, docstring, "
                "and handle edge cases (empty list, target not found). "
                "Also write 3 unit tests."
            )},
        ],
        max_tokens=800,
        quality_checks=["def binary_search", "return", "assert"],
    ),
    TestCase(
        category="code_gen",
        name="rest_api",
        messages=[
            {"role": "user", "content": (
                "Design and implement a FastAPI endpoint for a TODO app with:\n"
                "- POST /todos — create a todo (title, description, priority)\n"
                "- GET /todos — list todos with optional filter by priority\n"
                "- Pydantic models for request/response\n"
                "- Proper error handling and validation\n"
                "Write complete, production-quality code."
            )},
        ],
        max_tokens=1500,
        quality_checks=["FastAPI", "Pydantic", "def ", "class "],
    ),
    TestCase(
        category="code_gen",
        name="algorithm_lru",
        messages=[
            {"role": "user", "content": (
                "Implement an LRU Cache in Python from scratch (no functools.lru_cache). "
                "It should support get(key) and put(key, value) with O(1) time complexity. "
                "Use a doubly-linked list + hash map. Include full implementation with comments."
            )},
        ],
        max_tokens=1200,
        quality_checks=["class", "def get", "def put", "dict", "Node"],
    ),
]


# -- Tool calling --------------------------------------------------------------

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name, e.g. 'Tokyo' or 'New York, US'"
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "Temperature unit"
                    }
                },
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search for products in an online store",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_price": {"type": "number", "description": "Maximum price in USD"},
                    "category": {"type": "string", "description": "Product category"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a mathematical expression",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Math expression to evaluate"}
                },
                "required": ["expression"]
            }
        }
    }
]

TOOL_CALLING_CASES = [
    TestCase(
        category="tool_calling",
        name="weather_lookup",
        messages=[
            {"role": "system", "content": "You are a helpful assistant. Use the provided tools when appropriate."},
            {"role": "user", "content": "What's the weather like in Tokyo right now? Also check London."},
        ],
        max_tokens=300,
        tools=_TOOLS,
        quality_checks=["get_weather"],
    ),
    TestCase(
        category="tool_calling",
        name="product_search",
        messages=[
            {"role": "system", "content": "You are a shopping assistant. Use tools to find products."},
            {"role": "user", "content": "Find me wireless headphones under $100 in the electronics category."},
        ],
        max_tokens=300,
        tools=_TOOLS,
        quality_checks=["search_products"],
    ),
    TestCase(
        category="tool_calling",
        name="multi_tool",
        messages=[
            {"role": "system", "content": "You are a helpful assistant with access to tools."},
            {"role": "user", "content": (
                "I need to: 1) Check the weather in Paris, "
                "2) Calculate the expression (15 * 37) + 243, "
                "3) Search for running shoes under $150."
            )},
        ],
        max_tokens=500,
        tools=_TOOLS,
        quality_checks=["get_weather", "calculate", "search_products"],
    ),
]


ALL_TEST_CASES: List[TestCase] = (
    SHORT_CHAT_CASES + LONG_CHAT_CASES + REASONING_CASES + CODE_GEN_CASES + TOOL_CALLING_CASES
)


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------

@dataclass
class IterationResult:
    provider: str
    model: str
    category: str
    test_name: str
    iteration: int
    success: bool
    error: Optional[str] = None
    ttfb_ms: Optional[float] = None        # time to first byte (streaming)
    total_time_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    response_length: int = 0               # chars in response
    quality_score: float = 0.0             # 0.0 - 1.0 heuristic score
    response_preview: str = ""             # first 200 chars
    tool_calls_valid: bool = False


@dataclass
class BenchmarkSummary:
    provider: str
    model: str
    category: str
    total_iterations: int = 0
    successes: int = 0
    failures: int = 0
    success_rate: float = 0.0
    avg_ttfb_ms: float = 0.0
    p50_ttfb_ms: float = 0.0
    p95_ttfb_ms: float = 0.0
    avg_total_ms: float = 0.0
    p50_total_ms: float = 0.0
    p95_total_ms: float = 0.0
    avg_prompt_tokens: float = 0.0
    avg_completion_tokens: float = 0.0
    avg_total_tokens: float = 0.0
    avg_quality_score: float = 0.0


# ---------------------------------------------------------------------------
# Core benchmark engine
# ---------------------------------------------------------------------------

async def _run_single_iteration(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    model: str,
    test_case: TestCase,
    iteration: int,
) -> IterationResult:
    """Execute one iteration of a test case against a provider."""
    result = IterationResult(
        provider=provider.name,
        model=model,
        category=test_case.category,
        test_name=test_case.name,
        iteration=iteration,
        success=False,
    )

    payload: Dict[str, Any] = {
        "model": model,
        "messages": test_case.messages,
        "max_tokens": test_case.max_tokens,
        "stream": True,  # always stream for TTFB measurement
    }
    if test_case.tools:
        payload["tools"] = test_case.tools
        payload["tool_choice"] = "auto"

    t_start = time.monotonic()
    ttfb: Optional[float] = None
    collected_content: list[str] = []
    collected_tool_calls: list[dict] = []
    usage_data: Optional[dict] = None

    try:
        async with client.stream("POST", provider.base_url, json=payload, headers=provider.get_headers()) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                result.error = f"HTTP {resp.status_code}: {body.decode(errors='replace')[:300]}"
                result.total_time_ms = (time.monotonic() - t_start) * 1000
                return result

            # Record TTFB on first chunk
            first_chunk = True
            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue

                if first_chunk:
                    ttfb = (time.monotonic() - t_start) * 1000
                    first_chunk = False

                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Extract usage from final chunk
                if chunk.get("usage"):
                    usage_data = chunk["usage"]

                # Collect content
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    if delta.get("content"):
                        collected_content.append(delta["content"])
                    if delta.get("tool_calls"):
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            while len(collected_tool_calls) <= idx:
                                collected_tool_calls.append({"function": {"name": "", "arguments": ""}})
                            fn = tc.get("function", {})
                            if fn.get("name"):
                                collected_tool_calls[idx]["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                collected_tool_calls[idx]["function"]["arguments"] += fn["arguments"]

        t_end = time.monotonic()
        result.ttfb_ms = ttfb
        result.total_time_ms = (t_end - t_start) * 1000
        result.success = True

        # Token usage
        if usage_data:
            result.prompt_tokens = usage_data.get("prompt_tokens", 0)
            result.completion_tokens = usage_data.get("completion_tokens", 0)
            result.total_tokens = usage_data.get("total_tokens", 0)

        # Response text
        full_content = "".join(collected_content)
        result.response_length = len(full_content)
        result.response_preview = full_content[:200]

        # Quality scoring
        result.quality_score = _compute_quality(full_content, collected_tool_calls, test_case)

        # Tool call validation
        if test_case.tools and collected_tool_calls:
            tool_names = [tc["function"]["name"] for tc in collected_tool_calls if tc["function"]["name"]]
            result.tool_calls_valid = len(tool_names) > 0

    except httpx.ConnectError as e:
        result.error = f"Connection refused: {e}"
        result.total_time_ms = (time.monotonic() - t_start) * 1000
    except httpx.TimeoutException as e:
        result.error = f"Timeout: {e}"
        result.total_time_ms = (time.monotonic() - t_start) * 1000
    except Exception as e:
        result.error = f"{type(e).__name__}: {str(e)[:200]}"
        result.total_time_ms = (time.monotonic() - t_start) * 1000

    return result


def _compute_quality(content: str, tool_calls: list[dict], test_case: TestCase) -> float:
    """Heuristic quality score (0.0–1.0) based on response characteristics."""
    score = 0.0
    checks = 0

    # 1. Non-empty response (base score)
    if content.strip():
        score += 0.2
    checks += 1

    # 2. Minimum length (not too terse)
    min_len = {"short_chat": 10, "long_chat": 200, "reasoning": 50, "code_gen": 100, "tool_calling": 0}
    expected_min = min_len.get(test_case.category, 10)
    if len(content) >= expected_min:
        score += 0.2
    checks += 1

    # 3. Keyword quality checks
    if test_case.quality_checks:
        hits = sum(1 for kw in test_case.quality_checks if kw.lower() in content.lower())
        score += 0.4 * (hits / len(test_case.quality_checks))
        checks += 1

    # 4. Tool calling: did it call the expected tools?
    if test_case.tools and test_case.quality_checks:
        tool_names = [tc["function"]["name"] for tc in tool_calls if tc["function"]["name"]]
        expected_tools = [kw for kw in test_case.quality_checks if kw in [
            "get_weather", "search_products", "calculate"
        ]]
        if expected_tools:
            hits = sum(1 for fn in expected_tools if fn in tool_names)
            score += 0.4 * (hits / len(expected_tools))
            checks += 1

    # 5. Code generation: has function definition?
    if test_case.category == "code_gen":
        if "def " in content:
            score += 0.1
        checks += 1

    return min(score / max(checks, 1) * (checks / 3), 1.0) if checks > 0 else 0.0


def _compute_summary(results: List[IterationResult], provider: str, model: str, category: str) -> BenchmarkSummary:
    """Aggregate iteration results into a summary."""
    cat_results = [r for r in results if r.provider == provider and r.model == model and r.category == category]
    if not cat_results:
        return BenchmarkSummary(provider=provider, model=model, category=category)

    successes = [r for r in cat_results if r.success]
    failures = [r for r in cat_results if not r.success]

    ttfbs = [r.ttfb_ms for r in successes if r.ttfb_ms is not None]
    totals = [r.total_time_ms for r in successes]

    def _p50(vals):
        return statistics.median(vals) if vals else 0.0
    def _p95(vals):
        if not vals:
            return 0.0
        sorted_v = sorted(vals)
        idx = int(len(sorted_v) * 0.95)
        return sorted_v[min(idx, len(sorted_v) - 1)]

    return BenchmarkSummary(
        provider=provider,
        model=model,
        category=category,
        total_iterations=len(cat_results),
        successes=len(successes),
        failures=len(failures),
        success_rate=len(successes) / len(cat_results) * 100,
        avg_ttfb_ms=statistics.mean(ttfbs) if ttfbs else 0.0,
        p50_ttfb_ms=_p50(ttfbs),
        p95_ttfb_ms=_p95(ttfbs),
        avg_total_ms=statistics.mean(totals) if totals else 0.0,
        p50_total_ms=_p50(totals),
        p95_total_ms=_p95(totals),
        avg_prompt_tokens=statistics.mean([r.prompt_tokens for r in successes]) if successes else 0.0,
        avg_completion_tokens=statistics.mean([r.completion_tokens for r in successes]) if successes else 0.0,
        avg_total_tokens=statistics.mean([r.total_tokens for r in successes]) if successes else 0.0,
        avg_quality_score=statistics.mean([r.quality_score for r in successes]) if successes else 0.0,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_benchmark(
    providers: List[str],
    iterations: int,
    categories: Optional[List[str]] = None,
    concurrency: int = 1,
) -> tuple[List[IterationResult], List[BenchmarkSummary]]:
    """Run the full benchmark suite."""
    all_results: List[IterationResult] = []
    all_summaries: List[BenchmarkSummary] = []

    # Filter test cases by category
    test_cases = ALL_TEST_CASES
    if categories:
        test_cases = [tc for tc in ALL_TEST_CASES if tc.category in categories]

    if not test_cases:
        print("⚠️  No test cases match the specified categories.")
        return all_results, all_summaries

    print(f"\n{'='*70}")
    print(f"  PROVIDER BENCHMARK SUITE")
    print(f"  Providers: {', '.join(providers)}")
    print(f"  Iterations: {iterations}")
    print(f"  Test cases: {len(test_cases)}")
    print(f"  Categories: {', '.join(sorted(set(tc.category for tc in test_cases)))}")
    print(f"{'='*70}\n")

    for provider_name in providers:
        config = PROVIDERS.get(provider_name)
        if not config:
            print(f"⚠️  Unknown provider: {provider_name}")
            continue

        if config.requires_auth and not config.api_key:
            print(f"⚠️  Skipping {provider_name}: API key not configured")
            continue

        for model in config.models:
            print(f"\n▶ {provider_name} / {model}")
            print(f"  {'─'*60}")

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(config.timeout, connect=config.connect_timeout),
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
            ) as client:

                # Health check
                print(f"  Checking connectivity... ", end="", flush=True)
                try:
                    test_payload = {
                        "model": model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 5,
                    }
                    resp = await client.post(
                        config.base_url, json=test_payload, headers=config.get_headers()
                    )
                    if resp.status_code >= 400:
                        print(f"✗ HTTP {resp.status_code}")
                        # Record all as failures
                        for tc in test_cases:
                            for i in range(iterations):
                                all_results.append(IterationResult(
                                    provider=provider_name, model=model,
                                    category=tc.category, test_name=tc.name,
                                    iteration=i + 1, success=False,
                                    error=f"Health check failed: HTTP {resp.status_code}",
                                ))
                        continue
                    print(f"✓")
                except Exception as e:
                    print(f"✗ {e}")
                    for tc in test_cases:
                        for i in range(iterations):
                            all_results.append(IterationResult(
                                provider=provider_name, model=model,
                                category=tc.category, test_name=tc.name,
                                iteration=i + 1, success=False,
                                error=f"Health check failed: {e}",
                            ))
                    continue

                # Run test cases
                for tc in test_cases:
                    print(f"  [{tc.category}] {tc.name}...", end=" ", flush=True)
                    cat_results = []
                    for i in range(iterations):
                        result = await _run_single_iteration(client, config, model, tc, i + 1)
                        cat_results.append(result)
                        all_results.append(result)
                        # Small delay between iterations to avoid rate limiting
                        if i < iterations - 1:
                            await asyncio.sleep(0.5)

                    success_count = sum(1 for r in cat_results if r.success)
                    avg_time = statistics.mean([r.total_time_ms for r in cat_results if r.success]) if success_count > 0 else 0
                    print(f"{success_count}/{iterations} ok, avg {avg_time:.0f}ms")

            # Compute summaries for this provider/model
            for tc in test_cases:
                summary = _compute_summary(all_results, provider_name, model, tc.category)
                if summary.total_iterations > 0 and summary not in all_summaries:
                    # Avoid duplicates — check by key
                    key = (summary.provider, summary.model, summary.category)
                    if not any((s.provider, s.model, s.category) == key for s in all_summaries):
                        all_summaries.append(summary)

    return all_results, all_summaries


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_summary_table(summaries: List[BenchmarkSummary]) -> None:
    """Print a formatted summary table."""
    if not summaries:
        print("\n  No results to display.")
        return

    print(f"\n\n{'='*110}")
    print(f"  BENCHMARK RESULTS SUMMARY")
    print(f"{'='*110}")

    # Group by category
    categories = sorted(set(s.category for s in summaries))
    for cat in categories:
        cat_summaries = [s for s in summaries if s.category == cat]
        print(f"\n  ┌─ {cat.upper().replace('_', ' ')} {'─' * (100 - len(cat))}")
        print(f"  │ {'Provider':<20} {'Model':<35} {'Success':>8} {'Avg TTFB':>10} {'Avg Total':>10} {'Tokens':>8} {'Quality':>8}")
        print(f"  │ {'─'*20} {'─'*35} {'─'*8} {'─'*10} {'─'*10} {'─'*8} {'─'*8}")
        for s in cat_summaries:
            print(f"  │ {s.provider:<20} {s.model:<35} {s.success_rate:>6.0f}% {s.avg_ttfb_ms:>8.0f}ms {s.avg_total_ms:>8.0f}ms {s.avg_total_tokens:>6.0f} {s.avg_quality_score:>7.2f}")
        print(f"  └{'─'*108}")

    print(f"\n{'='*110}")

    # Cross-provider comparison
    print(f"\n  CROSS-PROVIDER COMPARISON (averages across all categories)")
    print(f"  {'─'*70}")
    providers_seen = sorted(set(s.provider for s in summaries))
    for p in providers_seen:
        p_summaries = [s for s in summaries if s.provider == p]
        if not p_summaries:
            continue
        avg_success = statistics.mean([s.success_rate for s in p_summaries])
        avg_ttfb = statistics.mean([s.avg_ttfb_ms for s in p_summaries if s.avg_ttfb_ms > 0])
        avg_total = statistics.mean([s.avg_total_ms for s in p_summaries if s.avg_total_ms > 0])
        avg_quality = statistics.mean([s.avg_quality_score for s in p_summaries])
        avg_tokens = statistics.mean([s.avg_total_tokens for s in p_summaries])
        print(f"  {p:<20} │ success={avg_success:.0f}% │ ttfb={avg_ttfb:.0f}ms │ total={avg_total:.0f}ms │ tokens={avg_tokens:.0f} │ quality={avg_quality:.2f}")
    print(f"  {'─'*70}")


def save_results(
    results: List[IterationResult],
    summaries: List[BenchmarkSummary],
    output_path: str,
) -> None:
    """Save full results to JSON."""
    output = {
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_iterations_per_case": max((r.iteration for r in results), default=0),
            "providers_tested": sorted(set(r.provider for r in results)),
            "total_results": len(results),
        },
        "summaries": [asdict(s) for s in summaries],
        "results": [asdict(r) for r in results],
    }
    Path(output_path).write_text(json.dumps(output, indent=2, default=str))
    print(f"\n  📄 Full results saved to: {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provider Benchmark Suite — compare LLM providers across categories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tests/benchmark.py
  python tests/benchmark.py --providers deepseek-bridge --iterations 5
  python tests/benchmark.py --providers deepinfra alibaba --output results.json
  python tests/benchmark.py --categories reasoning code_gen
  python tests/benchmark.py --list
        """,
    )
    parser.add_argument(
        "--providers", "-p",
        nargs="+",
        default=list(PROVIDERS.keys()),
        help=f"Providers to benchmark (default: all). Available: {', '.join(PROVIDERS.keys())}",
    )
    parser.add_argument(
        "--iterations", "-n",
        type=int,
        default=3,
        help="Number of iterations per test case (default: 3)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Path to save JSON results (default: benchmark_results_<timestamp>.json)",
    )
    parser.add_argument(
        "--categories", "-c",
        nargs="+",
        default=None,
        help="Filter by categories: short_chat, long_chat, reasoning, code_gen, tool_calling",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available providers and test cases, then exit",
    )
    return parser.parse_args()


def list_providers_and_tests() -> None:
    """Print available providers and test cases."""
    print("\n  AVAILABLE PROVIDERS")
    print(f"  {'─'*60}")
    for name, config in PROVIDERS.items():
        key_status = "✓ configured" if config.api_key else ("N/A (no auth)" if not config.requires_auth else "✗ missing key")
        print(f"  {name:<20} models: {', '.join(config.models)}")
        print(f"  {'':20} url:    {config.base_url}")
        print(f"  {'':20} auth:   {key_status}")
        print()

    print("\n  TEST CASES")
    print(f"  {'─'*60}")
    for cat in ["short_chat", "long_chat", "reasoning", "code_gen", "tool_calling"]:
        cases = [tc for tc in ALL_TEST_CASES if tc.category == cat]
        print(f"  [{cat}] ({len(cases)} cases)")
        for tc in cases:
            tools_tag = " +tools" if tc.tools else ""
            print(f"    • {tc.name}{tools_tag} (max_tokens={tc.max_tokens})")
    print()


def main() -> None:
    args = parse_args()

    if args.list:
        list_providers_and_tests()
        return

    # Validate providers
    for p in args.providers:
        if p not in PROVIDERS:
            print(f"❌ Unknown provider: {p}. Available: {', '.join(PROVIDERS.keys())}")
            sys.exit(1)

    # Run benchmark
    results, summaries = asyncio.run(
        run_benchmark(
            providers=args.providers,
            iterations=args.iterations,
            categories=args.categories,
        )
    )

    # Print summary
    print_summary_table(summaries)

    # Save results
    output_path = args.output
    if not output_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"benchmark_results_{ts}.json"
    save_results(results, summaries, output_path)


if __name__ == "__main__":
    main()
