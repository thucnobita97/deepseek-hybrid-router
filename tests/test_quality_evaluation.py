"""Quality evaluation tests for HybridResultAggregator.

Tests 10 different result set scenarios to evaluate aggregation quality
across coherence, completeness, redundancy elimination, conflict handling,
and code preservation.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from router.parallel_executor import ExecutionResult
from router.result_aggregator import (
    AggregatedResult,
    ConflictDetector,
    HybridResultAggregator,
    LLMResultAggregator,
    ResultAggregator,
)
from router.task_splitter import SubTask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockLLMAdapter:
    """Mock LLM adapter that returns configurable responses."""

    def __init__(self, response_content: str = "", should_fail: bool = False):
        self.response_content = response_content
        self.should_fail = should_fail
        self.call_count = 0
        self.last_request: Optional[Dict[str, Any]] = None

    async def send(self, request: Dict[str, Any], model: str) -> Dict[str, Any]:
        self.call_count += 1
        self.last_request = request

        if self.should_fail:
            raise ConnectionError("Simulated LLM failure")

        # If no custom content, generate a simple merged response
        content = self.response_content or "## Merged Response\n\nAll results combined successfully."

        return {
            "id": "mock-llm-response",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 150,
                "total_tokens": 650,
            },
        }


def make_result(
    sub_task_id: int,
    content: str,
    success: bool = True,
    error: Optional[str] = None,
    duration: float = 0.5,
    retries: int = 0,
) -> ExecutionResult:
    """Create an ExecutionResult with the given content."""
    if not success:
        return ExecutionResult(
            sub_task_id=sub_task_id,
            success=False,
            result=None,
            error=error or "Simulated error",
            duration=duration,
            retries=retries,
        )

    return ExecutionResult(
        sub_task_id=sub_task_id,
        success=True,
        result={
            "id": f"result-{sub_task_id}",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            },
        },
        error=None,
        duration=duration,
        retries=retries,
    )


def make_sub_task(
    task_id: int,
    description: str,
    depends_on: Optional[List[int]] = None,
    original_context: str = "Test task context",
) -> SubTask:
    """Create a SubTask with the given parameters."""
    return SubTask(
        id=task_id,
        description=description,
        depends_on=depends_on or [],
        original_context=original_context,
    )


# ---------------------------------------------------------------------------
# Test 1: Complementary Results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complementary_results():
    """Test aggregation of 3 results covering different aspects (performance, security, UX).

    Verification: All aspects are preserved in the aggregated result.
    """
    # Create 3 complementary results
    performance_result = make_result(
        sub_task_id=1,
        content=(
            "## Performance Analysis\n\n"
            "The application achieves 95th percentile response times of 120ms. "
            "CPU utilization averages 45% with peak memory usage at 512MB. "
            "Database query optimization reduced latency by 30%. "
            "Caching strategy improved throughput to 1,200 requests/second."
        ),
    )

    security_result = make_result(
        sub_task_id=2,
        content=(
            "## Security Assessment\n\n"
            "All endpoints use TLS 1.3 encryption. Authentication follows OAuth 2.0 "
            "with JWT tokens expiring after 15 minutes. SQL injection prevention via "
            "parameterized queries. XSS protection enabled with Content-Security-Policy headers. "
            "Regular security audits scheduled quarterly."
        ),
    )

    ux_result = make_result(
        sub_task_id=3,
        content=(
            "## User Experience Review\n\n"
            "Interface follows Material Design principles. Navigation requires max 3 clicks "
            "to reach any feature. Accessibility score: 98/100 (WCAG 2.1 AA compliant). "
            "Mobile responsiveness tested on iOS and Android. User satisfaction survey "
            "shows 4.5/5 average rating."
        ),
    )

    results = [performance_result, security_result, ux_result]
    sub_tasks = [
        make_sub_task(1, "Analyze performance metrics"),
        make_sub_task(2, "Assess security posture"),
        make_sub_task(3, "Review user experience"),
    ]

    # Use HybridResultAggregator with LLM
    llm_response = (
        "## Comprehensive System Review\n\n"
        "### Performance\n"
        "The application achieves 95th percentile response times of 120ms with "
        "1,200 requests/second throughput. CPU and memory usage are optimized.\n\n"
        "### Security\n"
        "Strong security posture with TLS 1.3, OAuth 2.0, and protection against "
        "common vulnerabilities. Regular audits ensure ongoing compliance.\n\n"
        "### User Experience\n"
        "Excellent UX with Material Design, accessibility compliance (98/100), "
        "and high user satisfaction (4.5/5 rating)."
    )

    adapter = MockLLMAdapter(response_content=llm_response)
    aggregator = HybridResultAggregator(adapter=adapter, use_llm=True)

    aggregated = await aggregator.aggregate(results, sub_tasks)

    # Verify all aspects are preserved
    assert aggregated.success_count == 3
    assert aggregated.failure_count == 0

    # Check that key information from each aspect is present
    combined = aggregated.combined_response.lower()
    assert "performance" in combined or "120ms" in combined
    assert "security" in combined or "tls" in combined or "oauth" in combined
    assert "user experience" in combined or "ux" in combined or "accessibility" in combined

    # Verify no important details lost
    assert "1,200" in combined or "throughput" in combined
    assert "98" in combined or "accessibility" in combined


# ---------------------------------------------------------------------------
# Test 2: Conflicting Results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conflicting_results():
    """Test aggregation of 2 results with contradictions.

    Verification: Conflict is detected and handled appropriately.
    """
    # Create 2 conflicting results
    result_a = make_result(
        sub_task_id=1,
        content=(
            "## Database Migration Plan\n\n"
            "Delete the legacy users table and create a new normalized schema. "
            "Remove all deprecated columns. The migration script will drop and "
            "recreate the table structure."
        ),
    )

    result_b = make_result(
        sub_task_id=2,
        content=(
            "## Data Preservation Strategy\n\n"
            "Keep the existing users table and add new columns incrementally. "
            "Preserve all historical data. Create the new schema alongside the "
            "old one to maintain backward compatibility."
        ),
    )

    results = [result_a, result_b]
    sub_tasks = [
        make_sub_task(1, "Plan database migration"),
        make_sub_task(2, "Define data preservation strategy"),
    ]

    # Use rule-based aggregator to test conflict detection
    aggregator = HybridResultAggregator(adapter=None, use_llm=False)
    aggregated = await aggregator.aggregate(results, sub_tasks)

    # Verify conflict is detected
    assert len(aggregated.conflicts) > 0, "Expected conflict to be detected"

    # Check that conflict mentions the contradiction
    conflict_text = " ".join(aggregated.conflicts).lower()
    assert "delete" in conflict_text or "remove" in conflict_text or "contradiction" in conflict_text

    # Verify both perspectives are still included in the response
    combined = aggregated.combined_response.lower()
    assert "delete" in combined or "drop" in combined
    assert "keep" in combined or "preserve" in combined


# ---------------------------------------------------------------------------
# Test 3: Overlapping Results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlapping_results():
    """Test aggregation of 3 results with ~50% overlap.

    Verification: Redundancy is eliminated while preserving unique information.
    """
    # Create 3 results with overlapping content
    result_1 = make_result(
        sub_task_id=1,
        content=(
            "## API Documentation\n\n"
            "The REST API uses JSON for request and response bodies. "
            "Authentication is via Bearer tokens in the Authorization header. "
            "Rate limiting is set to 100 requests per minute. "
            "All endpoints return standard HTTP status codes."
        ),
    )

    result_2 = make_result(
        sub_task_id=2,
        content=(
            "## API Implementation Details\n\n"
            "The REST API uses JSON for request and response bodies. "
            "Authentication is via Bearer tokens in the Authorization header. "
            "Endpoints are versioned using URL paths (e.g., /v1/users). "
            "Request validation uses JSON Schema."
        ),
    )

    result_3 = make_result(
        sub_task_id=3,
        content=(
            "## API Security\n\n"
            "Authentication is via Bearer tokens in the Authorization header. "
            "All endpoints require HTTPS. API keys are rotated every 90 days. "
            "Rate limiting is set to 100 requests per minute. "
            "CORS is configured to allow only whitelisted domains."
        ),
    )

    results = [result_1, result_2, result_3]
    sub_tasks = [
        make_sub_task(1, "Document API basics"),
        make_sub_task(2, "Document API implementation"),
        make_sub_task(3, "Document API security"),
    ]

    # Use LLM to merge and eliminate redundancy
    llm_response = (
        "## Comprehensive API Documentation\n\n"
        "### Core Features\n"
        "The REST API uses JSON for all requests and responses. Authentication "
        "uses Bearer tokens in the Authorization header. Rate limiting is enforced "
        "at 100 requests per minute.\n\n"
        "### Implementation\n"
        "Endpoints are versioned via URL paths (e.g., /v1/users). Request validation "
        "uses JSON Schema. All endpoints return standard HTTP status codes.\n\n"
        "### Security\n"
        "HTTPS is required for all endpoints. API keys rotate every 90 days. "
        "CORS restricts access to whitelisted domains only."
    )

    adapter = MockLLMAdapter(response_content=llm_response)
    aggregator = HybridResultAggregator(adapter=adapter, use_llm=True)

    aggregated = await aggregator.aggregate(results, sub_tasks)

    # Verify successful aggregation
    assert aggregated.success_count == 3

    # Check that common information appears only once (not 3 times)
    combined = aggregated.combined_response
    bearer_count = combined.lower().count("bearer token")
    rate_limit_count = combined.lower().count("100 requests per minute")

    # LLM should have deduplicated - allow up to 2 mentions for safety
    assert bearer_count <= 2, f"'Bearer token' appears {bearer_count} times, expected <= 2"
    assert rate_limit_count <= 2, f"'100 requests per minute' appears {rate_limit_count} times"

    # Verify unique information from each result is preserved
    assert "json schema" in combined.lower() or "validation" in combined.lower()
    assert "cors" in combined.lower() or "whitelist" in combined.lower()
    assert "versioned" in combined.lower() or "/v1/" in combined


# ---------------------------------------------------------------------------
# Test 4: Partial Failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_failures():
    """Test aggregation of 5 results with 2 failures.

    Verification: Successful results are prioritized and failures are noted.
    """
    # Create 5 results: 3 successful, 2 failed
    results = [
        make_result(1, "Task 1 completed successfully with full data.", success=True),
        make_result(2, "Task 2 failed due to timeout", success=False, error="Timeout after 30s"),
        make_result(3, "Task 3 completed with partial results.", success=True),
        make_result(4, "Task 4 failed: connection refused", success=False, error="Connection refused"),
        make_result(5, "Task 5 completed successfully.", success=True),
    ]

    sub_tasks = [
        make_sub_task(i, f"Execute task {i}") for i in range(1, 6)
    ]

    aggregator = HybridResultAggregator(adapter=None, use_llm=False)
    aggregated = await aggregator.aggregate(results, sub_tasks)

    # Verify counts
    assert aggregated.success_count == 3
    assert aggregated.failure_count == 2

    # Check that successful results are included
    combined = aggregated.combined_response.lower()
    assert "task 1" in combined or "completed successfully" in combined
    assert "task 3" in combined or "partial results" in combined
    assert "task 5" in combined

    # Check that failures are noted
    assert "⚠️" in aggregated.combined_response or "failed" in combined
    assert "timeout" in combined or "connection" in combined

    # Verify metadata reflects the partial success
    assert aggregated.metadata.get("success_count") == 3
    assert aggregated.metadata.get("failure_count") == 2


# ---------------------------------------------------------------------------
# Test 5: Empty Results (All Failed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_results():
    """Test aggregation when all results failed.

    Verification: Graceful error message is provided.
    """
    # Create all-failed results
    results = [
        make_result(1, "", success=False, error="Database connection failed"),
        make_result(2, "", success=False, error="API timeout"),
        make_result(3, "", success=False, error="Service unavailable"),
    ]

    sub_tasks = [
        make_sub_task(i, f"Task {i}") for i in range(1, 4)
    ]

    aggregator = HybridResultAggregator(adapter=None, use_llm=False)
    aggregated = await aggregator.aggregate(results, sub_tasks)

    # Verify all failed
    assert aggregated.success_count == 0
    assert aggregated.failure_count == 3

    # Check for error summary
    assert "All sub-tasks failed" in aggregated.combined_response
    assert "None of the sub-tasks completed successfully" in aggregated.combined_response

    # Verify individual errors are listed
    combined = aggregated.combined_response.lower()
    assert "database" in combined or "connection" in combined
    assert "timeout" in combined
    assert "unavailable" in combined

    # Check that total duration is reported
    assert "duration" in combined or "total" in combined


# ---------------------------------------------------------------------------
# Test 6: Code Blocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_code_blocks():
    """Test aggregation of results with code snippets.

    Verification: Code blocks are preserved intact.
    """
    # Create results with code
    result_1 = make_result(
        sub_task_id=1,
        content=(
            "## Python Implementation\n\n"
            "Here's the function:\n\n"
            "```python\n"
            "def calculate_total(items):\n"
            "    \"\"\"Calculate total price with tax.\"\"\"\n"
            "    subtotal = sum(item.price for item in items)\n"
            "    tax = subtotal * 0.08\n"
            "    return subtotal + tax\n"
            "```\n\n"
            "This function handles the core calculation logic."
        ),
    )

    result_2 = make_result(
        sub_task_id=2,
        content=(
            "## JavaScript Frontend\n\n"
            "The client-side code:\n\n"
            "```javascript\n"
            "async function fetchTotal(items) {\n"
            "  const response = await fetch('/api/calculate', {\n"
            "    method: 'POST',\n"
            "    body: JSON.stringify({ items })\n"
            "  });\n"
            "  return response.json();\n"
            "}\n"
            "```\n\n"
            "This makes the API call to get the total."
        ),
    )

    result_3 = make_result(
        sub_task_id=3,
        content=(
            "## Configuration\n\n"
            "Database setup:\n\n"
            "```yaml\n"
            "database:\n"
            "  host: localhost\n"
            "  port: 5432\n"
            "  name: shop_db\n"
            "  pool_size: 10\n"
            "```\n\n"
            "These settings configure the database connection."
        ),
    )

    results = [result_1, result_2, result_3]
    sub_tasks = [
        make_sub_task(1, "Implement backend logic"),
        make_sub_task(2, "Implement frontend integration"),
        make_sub_task(3, "Configure database"),
    ]

    # Use LLM that preserves code
    llm_response = (
        "## Complete Implementation Guide\n\n"
        "### Backend (Python)\n"
        "```python\n"
        "def calculate_total(items):\n"
        "    \"\"\"Calculate total price with tax.\"\"\"\n"
        "    subtotal = sum(item.price for item in items)\n"
        "    tax = subtotal * 0.08\n"
        "    return subtotal + tax\n"
        "```\n\n"
        "### Frontend (JavaScript)\n"
        "```javascript\n"
        "async function fetchTotal(items) {\n"
        "  const response = await fetch('/api/calculate', {\n"
        "    method: 'POST',\n"
        "    body: JSON.stringify({ items })\n"
        "  });\n"
        "  return response.json();\n"
        "}\n"
        "```\n\n"
        "### Configuration\n"
        "```yaml\n"
        "database:\n"
        "  host: localhost\n"
        "  port: 5432\n"
        "  name: shop_db\n"
        "  pool_size: 10\n"
        "```"
    )

    adapter = MockLLMAdapter(response_content=llm_response)
    aggregator = HybridResultAggregator(adapter=adapter, use_llm=True)

    aggregated = await aggregator.aggregate(results, sub_tasks)

    # Verify code blocks are preserved
    combined = aggregated.combined_response

    # Check for Python code
    assert "```python" in combined
    assert "def calculate_total" in combined
    assert "subtotal * 0.08" in combined

    # Check for JavaScript code
    assert "```javascript" in combined
    assert "async function fetchTotal" in combined
    assert "JSON.stringify" in combined

    # Check for YAML code
    assert "```yaml" in combined
    assert "port: 5432" in combined
    assert "pool_size: 10" in combined

    # Verify code block structure is intact
    assert combined.count("```") >= 6  # At least 3 opening + 3 closing


# ---------------------------------------------------------------------------
# Test 7: Long Results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_results():
    """Test aggregation of 3 very long results (~2000 chars each).

    Verification: Key points are extracted and summarized.
    """
    # Create long results
    long_text_1 = (
        "## Detailed Performance Report\n\n"
        + "This is a very detailed performance analysis. " * 50
        + "Key finding: Response time improved by 45% after optimization. "
        + "The database indexing strategy reduced query times from 500ms to 50ms. "
        + "Load testing showed the system can handle 10,000 concurrent users."
    )

    long_text_2 = (
        "## Comprehensive Security Audit\n\n"
        + "This section covers extensive security testing. " * 50
        + "Critical finding: All SQL injection vulnerabilities patched. "
        + "XSS protection implemented across all input fields. "
        + "Penetration testing revealed no critical issues remaining."
    )

    long_text_3 = (
        "## User Acceptance Testing Results\n\n"
        + "Detailed UAT feedback from 50 participants. " * 50
        + "Key insight: 92% of users completed tasks successfully. "
        + "Average task completion time reduced from 5 minutes to 2 minutes. "
        + "User satisfaction score improved from 3.2 to 4.6 out of 5."
    )

    results = [
        make_result(1, long_text_1),
        make_result(2, long_text_2),
        make_result(3, long_text_3),
    ]

    sub_tasks = [
        make_sub_task(1, "Analyze performance"),
        make_sub_task(2, "Audit security"),
        make_sub_task(3, "Conduct UAT"),
    ]

    # LLM summarizes key points
    llm_response = (
        "## Executive Summary\n\n"
        "### Performance\n"
        "Response time improved by 45%. Database optimization reduced query times "
        "from 500ms to 50ms. System supports 10,000 concurrent users.\n\n"
        "### Security\n"
        "All SQL injection vulnerabilities patched. XSS protection implemented. "
        "Penetration testing shows no critical issues.\n\n"
        "### User Acceptance\n"
        "92% task completion rate. Average time reduced from 5 to 2 minutes. "
        "Satisfaction improved from 3.2 to 4.6 out of 5."
    )

    adapter = MockLLMAdapter(response_content=llm_response)
    aggregator = HybridResultAggregator(adapter=adapter, use_llm=True)

    aggregated = await aggregator.aggregate(results, sub_tasks)

    # Verify key points are extracted
    combined = aggregated.combined_response.lower()

    # Check performance key points
    assert "45%" in combined or "response time" in combined
    assert "10,000" in combined or "concurrent" in combined

    # Check security key points
    assert "sql injection" in combined
    assert "xss" in combined or "protection" in combined

    # Check UAT key points
    assert "92%" in combined or "task completion" in combined
    assert "4.6" in combined or "satisfaction" in combined

    # Verify the response is significantly shorter than input
    total_input_length = len(long_text_1) + len(long_text_2) + len(long_text_3)
    assert len(combined) < total_input_length, "LLM should have summarized long inputs"


# ---------------------------------------------------------------------------
# Test 8: Structured Data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_data():
    """Test aggregation of results with JSON and tables.

    Verification: Structured data format is maintained.
    """
    # Create results with structured data
    json_result = make_result(
        sub_task_id=1,
        content=(
            "## API Response Schema\n\n"
            "```json\n"
            "{\n"
            '  "user": {\n'
            '    "id": 123,\n'
            '    "name": "John Doe",\n'
            '    "email": "john@example.com",\n'
            '    "roles": ["admin", "user"]\n'
            "  }\n"
            "}\n"
            "```\n\n"
            "This is the standard user response format."
        ),
    )

    table_result = make_result(
        sub_task_id=2,
        content=(
            "## Feature Comparison\n\n"
            "| Feature | Basic | Pro | Enterprise |\n"
            "|---------|-------|-----|------------|\n"
            "| Users   | 10    | 100 | Unlimited  |\n"
            "| Storage | 1GB   | 10GB| 100GB      |\n"
            "| Support | Email | 24/7| Dedicated  |\n\n"
            "This table shows the pricing tiers."
        ),
    )

    list_result = make_result(
        sub_task_id=3,
        content=(
            "## Configuration Options\n\n"
            "```ini\n"
            "[server]\n"
            "host = 0.0.0.0\n"
            "port = 8080\n"
            "workers = 4\n\n"
            "[database]\n"
            "pool_size = 20\n"
            "timeout = 30\n"
            "```\n\n"
            "These are the main configuration parameters."
        ),
    )

    results = [json_result, table_result, list_result]
    sub_tasks = [
        make_sub_task(1, "Define API schema"),
        make_sub_task(2, "Compare features"),
        make_sub_task(3, "Document configuration"),
    ]

    # LLM preserves structure
    llm_response = (
        "## System Documentation\n\n"
        "### API Schema\n"
        "```json\n"
        "{\n"
        '  "user": {\n'
        '    "id": 123,\n'
        '    "name": "John Doe",\n'
        '    "email": "john@example.com",\n'
        '    "roles": ["admin", "user"]\n'
        "  }\n"
        "}\n"
        "```\n\n"
        "### Pricing Tiers\n"
        "| Feature | Basic | Pro | Enterprise |\n"
        "|---------|-------|-----|------------|\n"
        "| Users   | 10    | 100 | Unlimited  |\n"
        "| Storage | 1GB   | 10GB| 100GB      |\n"
        "| Support | Email | 24/7| Dedicated  |\n\n"
        "### Configuration\n"
        "```ini\n"
        "[server]\n"
        "host = 0.0.0.0\n"
        "port = 8080\n"
        "workers = 4\n\n"
        "[database]\n"
        "pool_size = 20\n"
        "timeout = 30\n"
        "```"
    )

    adapter = MockLLMAdapter(response_content=llm_response)
    aggregator = HybridResultAggregator(adapter=adapter, use_llm=True)

    aggregated = await aggregator.aggregate(results, sub_tasks)

    combined = aggregated.combined_response

    # Verify JSON structure preserved
    assert "```json" in combined
    assert '"user"' in combined
    assert '"roles"' in combined

    # Verify table structure preserved
    assert "|" in combined
    assert "Feature" in combined
    assert "Enterprise" in combined
    assert "Unlimited" in combined

    # Verify INI structure preserved
    assert "```ini" in combined
    assert "[server]" in combined
    assert "[database]" in combined
    assert "pool_size" in combined


# ---------------------------------------------------------------------------
# Test 9: Multilingual
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multilingual():
    """Test aggregation of results in different languages.

    Verification: Languages are not mixed inappropriately.
    """
    # Create results in different languages
    english_result = make_result(
        sub_task_id=1,
        content=(
            "## English Documentation\n\n"
            "This guide explains how to install the application. "
            "First, download the installer from our website. "
            "Then, run the setup wizard and follow the instructions. "
            "The installation takes approximately 5 minutes."
        ),
    )

    spanish_result = make_result(
        sub_task_id=2,
        content=(
            "## Documentación en Español\n\n"
            "Esta guía explica cómo instalar la aplicación. "
            "Primero, descargue el instalador de nuestro sitio web. "
            "Luego, ejecute el asistente de configuración y siga las instrucciones. "
            "La instalación tarda aproximadamente 5 minutos."
        ),
    )

    french_result = make_result(
        sub_task_id=3,
        content=(
            "## Documentation en Français\n\n"
            "Ce guide explique comment installer l'application. "
            "Tout d'abord, téléchargez l'installateur depuis notre site web. "
            "Ensuite, exécutez l'assistant d'installation et suivez les instructions. "
            "L'installation prend environ 5 minutes."
        ),
    )

    results = [english_result, spanish_result, french_result]
    sub_tasks = [
        make_sub_task(1, "English documentation"),
        make_sub_task(2, "Spanish documentation"),
        make_sub_task(3, "French documentation"),
    ]

    # LLM keeps languages separate
    llm_response = (
        "## Multilingual Installation Guide\n\n"
        "### English\n"
        "This guide explains how to install the application. Download the installer, "
        "run the setup wizard, and follow instructions. Takes ~5 minutes.\n\n"
        "### Español\n"
        "Esta guía explica cómo instalar la aplicación. Descargue el instalador, "
        "ejecute el asistente y siga las instrucciones. Tarda ~5 minutos.\n\n"
        "### Français\n"
        "Ce guide explique comment installer l'application. Téléchargez l'installateur, "
        "exécutez l'assistant et suivez les instructions. Prend ~5 minutes."
    )

    adapter = MockLLMAdapter(response_content=llm_response)
    aggregator = HybridResultAggregator(adapter=adapter, use_llm=True)

    aggregated = await aggregator.aggregate(results, sub_tasks)

    combined = aggregated.combined_response

    # Verify each language section exists
    assert "English" in combined or "english" in combined.lower()
    assert "Español" in combined or "español" in combined.lower()
    assert "Français" in combined or "français" in combined.lower()

    # Verify languages are not inappropriately mixed
    # (e.g., Spanish words shouldn't appear in English section)
    english_section = combined[combined.find("English"):combined.find("Español")]
    assert "descargue" not in english_section.lower()
    assert "téléchargez" not in english_section.lower()

    # Verify key content in each language
    assert "install" in combined.lower() or "installer" in combined.lower()
    assert "5 minutes" in combined or "5 minutos" in combined or "5 minutes" in combined


# ---------------------------------------------------------------------------
# Test 10: Priority Ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_priority_ordering():
    """Test aggregation of results with different quality levels.

    Verification: High-quality results are prioritized in the output.
    """
    # Create results with varying quality
    high_quality = make_result(
        sub_task_id=1,
        content=(
            "## Critical Security Patch\n\n"
            "CVE-2024-1234 has been patched. The vulnerability allowed remote code "
            "execution via buffer overflow. All systems must be updated immediately. "
            "Patch verification completed with 100% test coverage."
        ),
        duration=0.3,
    )

    medium_quality = make_result(
        sub_task_id=2,
        content=(
            "## Feature Enhancement\n\n"
            "Added dark mode support to the UI. Users can toggle between light "
            "and dark themes in settings. Feature tested on Chrome and Firefox."
        ),
        duration=0.5,
    )

    low_quality = make_result(
        sub_task_id=3,
        content=(
            "## Minor UI Fix\n\n"
            "Fixed typo in login page. Changed 'Sing In' to 'Sign In'. "
            "Cosmetic change only."
        ),
        duration=0.2,
    )

    results = [high_quality, medium_quality, low_quality]
    sub_tasks = [
        make_sub_task(1, "Apply security patch"),
        make_sub_task(2, "Add dark mode feature"),
        make_sub_task(3, "Fix typo"),
    ]

    # LLM prioritizes by importance
    llm_response = (
        "## Update Summary\n\n"
        "### 🔴 Critical: Security Patch\n"
        "CVE-2024-1234 patched - remote code execution vulnerability fixed. "
        "Immediate deployment required. 100% test coverage verified.\n\n"
        "### 🟡 Enhancement: Dark Mode\n"
        "New dark mode feature added with theme toggle in settings. "
        "Tested on Chrome and Firefox.\n\n"
        "### 🟢 Minor: UI Fix\n"
        "Typo corrected on login page ('Sing In' → 'Sign In')."
    )

    adapter = MockLLMAdapter(response_content=llm_response)
    aggregator = HybridResultAggregator(adapter=adapter, use_llm=True)

    aggregated = await aggregator.aggregate(results, sub_tasks)

    combined = aggregated.combined_response

    # Verify all results are included
    assert "CVE-2024-1234" in combined or "security" in combined.lower()
    assert "dark mode" in combined.lower() or "theme" in combined.lower()
    assert "typo" in combined.lower() or "sign in" in combined.lower()

    # Verify priority indicators (if present)
    # The high-priority security patch should appear first or be emphasized
    security_pos = combined.lower().find("security") if "security" in combined.lower() else combined.lower().find("cve")
    dark_mode_pos = combined.lower().find("dark mode")
    typo_pos = combined.lower().find("typo")

    # Security should come before dark mode and typo (higher priority)
    if security_pos >= 0 and dark_mode_pos >= 0 and typo_pos >= 0:
        assert security_pos < dark_mode_pos, "Security patch should be prioritized over feature"
        assert security_pos < typo_pos, "Security patch should be prioritized over minor fix"

    # Verify the response flows naturally
    assert aggregated.success_count == 3
    assert aggregated.failure_count == 0


# ---------------------------------------------------------------------------
# Additional Quality Metrics Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coherence_metric():
    """Test that aggregated results flow naturally."""
    results = [
        make_result(1, "First part of the analysis."),
        make_result(2, "Second part continues the analysis."),
        make_result(3, "Final part concludes the analysis."),
    ]

    llm_response = (
        "## Complete Analysis\n\n"
        "The analysis begins with the first part, continues with additional "
        "findings in the second part, and concludes with final recommendations."
    )

    adapter = MockLLMAdapter(response_content=llm_response)
    aggregator = HybridResultAggregator(adapter=adapter, use_llm=True)

    aggregated = await aggregator.aggregate(results)

    # Check for natural flow indicators
    combined = aggregated.combined_response.lower()
    assert "begins" in combined or "first" in combined
    assert "continues" in combined or "second" in combined
    assert "concludes" in combined or "final" in combined


@pytest.mark.asyncio
async def test_completeness_metric():
    """Test that no important information is lost."""
    important_data = "CRITICAL: Database backup completed at 2024-01-15 14:30:00 UTC"

    results = [
        make_result(1, important_data),
        make_result(2, "Additional context about the backup process."),
    ]

    llm_response = (
        "## Backup Report\n\n"
        "CRITICAL: Database backup completed at 2024-01-15 14:30:00 UTC. "
        "The backup process included verification and integrity checks."
    )

    adapter = MockLLMAdapter(response_content=llm_response)
    aggregator = HybridResultAggregator(adapter=adapter, use_llm=True)

    aggregated = await aggregator.aggregate(results)

    # Verify critical information is preserved
    assert "CRITICAL" in aggregated.combined_response
    assert "2024-01-15" in aggregated.combined_response
    assert "14:30:00" in aggregated.combined_response


@pytest.mark.asyncio
async def test_redundancy_elimination():
    """Test that duplicate information is eliminated."""
    repeated_info = "The system requires 8GB of RAM minimum."

    results = [
        make_result(1, f"{repeated_info} Additional: CPU must be 4-core."),
        make_result(2, f"{repeated_info} Additional: SSD storage required."),
        make_result(3, f"{repeated_info} Additional: 64-bit OS required."),
    ]

    llm_response = (
        "## System Requirements\n\n"
        "The system requires 8GB of RAM minimum. Additionally:\n"
        "- CPU must be 4-core\n"
        "- SSD storage required\n"
        "- 64-bit OS required"
    )

    adapter = MockLLMAdapter(response_content=llm_response)
    aggregator = HybridResultAggregator(adapter=adapter, use_llm=True)

    aggregated = await aggregator.aggregate(results)

    # The repeated phrase should appear only once
    count = aggregated.combined_response.count("8GB of RAM minimum")
    assert count == 1, f"Expected '8GB of RAM minimum' to appear once, but it appears {count} times"

    # But unique additions should all be present
    combined = aggregated.combined_response.lower()
    assert "4-core" in combined
    assert "ssd" in combined
    assert "64-bit" in combined


# ---------------------------------------------------------------------------
# Test runner for standalone execution
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import asyncio

    async def run_all_tests():
        """Run all quality evaluation tests."""
        tests = [
            ("Complementary Results", test_complementary_results),
            ("Conflicting Results", test_conflicting_results),
            ("Overlapping Results", test_overlapping_results),
            ("Partial Failures", test_partial_failures),
            ("Empty Results", test_empty_results),
            ("Code Blocks", test_code_blocks),
            ("Long Results", test_long_results),
            ("Structured Data", test_structured_data),
            ("Multilingual", test_multilingual),
            ("Priority Ordering", test_priority_ordering),
            ("Coherence Metric", test_coherence_metric),
            ("Completeness Metric", test_completeness_metric),
            ("Redundancy Elimination", test_redundancy_elimination),
        ]

        print("=" * 70)
        print("Quality Evaluation Tests for HybridResultAggregator")
        print("=" * 70)

        passed = 0
        failed = 0

        for name, test_func in tests:
            try:
                await test_func()
                print(f"✓ {name}")
                passed += 1
            except AssertionError as e:
                print(f"✗ {name}: {e}")
                failed += 1
            except Exception as e:
                print(f"✗ {name}: Unexpected error: {e}")
                failed += 1

        print("\n" + "=" * 70)
        print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")
        print("=" * 70)

        return failed == 0

    success = asyncio.run(run_all_tests())
    exit(0 if success else 1)
