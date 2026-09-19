"""Comprehensive tests for TaskSizeDetector (Task 1.1 verification).

Tests the rule-based task analysis heuristics covering:
- Text extraction from dict and object inputs
- Token estimation accuracy
- Intent detection for all supported intents
- Complexity classification (simple / medium / complex)
- Split decision logic (threshold, complexity, multi-file, numbered steps)
- Split count suggestions
- Edge cases: empty input, non-English text, code blocks, multi-part content
"""
from __future__ import annotations

from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from router.task_splitter import (
    TaskSizeDetector,
    TaskAnalysis,
    TOKEN_SPLIT_THRESHOLD,
    _estimate_tokens,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Msg:
    """Lightweight message object mimicking ChatCompletionRequest messages."""
    def __init__(self, role: str, content):
        self.role = role
        self.content = content


class _Req:
    """Lightweight request object mimicking ChatCompletionRequest."""
    def __init__(self, messages: list):
        self.messages = messages


def _dict_req(text: str, role: str = "user") -> dict:
    """Build a dict-style request with a single user message."""
    return {"messages": [{"role": role, "content": text}]}


def _obj_req(text: str, role: str = "user") -> _Req:
    """Build an object-style request with a single user message."""
    return _Req([_Msg(role, text)])


def _generate_words(n: int) -> str:
    """Generate approximately n words of filler text."""
    base = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore magna aliqua"
    words = base.split()
    result = []
    for i in range(n):
        result.append(words[i % len(words)])
    return " ".join(result)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def detector():
    return TaskSizeDetector()


# ---------------------------------------------------------------------------
# 1. test_empty_message - empty content returns simple, no split
# ---------------------------------------------------------------------------


class TestEmptyMessage:
    def test_empty_string(self, detector):
        req = _dict_req("")
        a = detector.analyze(req)
        assert not a.needs_splitting
        assert a.complexity == "simple"
        assert a.estimated_tokens == 0 or a.estimated_tokens <= 1
        assert a.suggested_split_count == 1

    def test_empty_messages_list(self, detector):
        req = {"messages": []}
        a = detector.analyze(req)
        assert not a.needs_splitting
        assert a.complexity == "simple"

    def test_none_content(self, detector):
        req = {"messages": [{"role": "user", "content": None}]}
        a = detector.analyze(req)
        assert not a.needs_splitting


# ---------------------------------------------------------------------------
# 2. test_very_short_message - 'hello' returns simple, no split
# ---------------------------------------------------------------------------


class TestVeryShortMessage:
    def test_hello(self, detector):
        a = detector.analyze(_dict_req("hello"))
        assert not a.needs_splitting
        assert a.complexity == "simple"
        assert a.estimated_tokens < 10
        assert a.suggested_split_count == 1

    def test_single_word(self, detector):
        a = detector.analyze(_dict_req("yes"))
        assert not a.needs_splitting
        assert a.complexity == "simple"


# ---------------------------------------------------------------------------
# 3. test_single_sentence - one sentence, simple, no split
# ---------------------------------------------------------------------------


class TestSingleSentence:
    def test_simple_question(self, detector):
        a = detector.analyze(_dict_req("What is the capital of France?"))
        assert not a.needs_splitting
        assert a.complexity == "simple"
        assert a.suggested_split_count == 1

    def test_simple_statement(self, detector):
        a = detector.analyze(_dict_req("Please fix the typo in the readme file."))
        assert not a.needs_splitting
        assert a.complexity == "simple"


# ---------------------------------------------------------------------------
# 4. test_medium_paragraph - ~200 words, medium, no split
# ---------------------------------------------------------------------------


class TestMediumParagraph:
    def test_200_words_no_split(self, detector):
        text = _generate_words(200)
        a = detector.analyze(_dict_req(text))
        # ~200 words ≈ ~260 tokens (at 1.3x) or ~200 tokens with tiktoken
        # Below 4000 threshold, no complexity keywords → no split
        assert not a.needs_splitting
        assert a.estimated_tokens < TOKEN_SPLIT_THRESHOLD

    def test_medium_with_keywords_may_be_medium_complexity(self, detector):
        # Add some complexity keywords to push complexity to "medium"
        text = _generate_words(200) + " step by step in order sequentially"
        a = detector.analyze(_dict_req(text))
        # With pattern hits, complexity might be medium but still no split
        # (unless complexity reaches "complex")
        assert a.estimated_tokens < TOKEN_SPLIT_THRESHOLD


# ---------------------------------------------------------------------------
# 5. test_large_document_4000plus - >4000 tokens, needs split
# ---------------------------------------------------------------------------


class TestLargeDocument4000Plus:
    def test_4000_tokens_triggers_split(self, detector):
        # ~3100 words * 1.3 ≈ 4030 tokens (fallback), or generate enough for tiktoken
        text = _generate_words(3500)
        a = detector.analyze(_dict_req(text))
        assert a.needs_splitting
        assert a.estimated_tokens > TOKEN_SPLIT_THRESHOLD
        assert a.suggested_split_count >= 2

    def test_split_count_scales_with_tokens(self, detector):
        text = _generate_words(5000)
        a = detector.analyze(_dict_req(text))
        assert a.needs_splitting
        # ~6500 tokens / 2000 ≈ 3 chunks
        assert a.suggested_split_count >= 3


# ---------------------------------------------------------------------------
# 6. test_very_large_8000plus - >8000 tokens, needs split, high count
# ---------------------------------------------------------------------------


class TestVeryLarge8000Plus:
    def test_8000_tokens_high_split_count(self, detector):
        # Use unique words to ensure high token count; add complexity keywords
        text = " ".join(f"word{i:04d}" for i in range(12000))
        text += " every module all files entire codebase"
        a = detector.analyze(_dict_req(text))
        assert a.needs_splitting
        assert a.estimated_tokens > 8000
        assert a.complexity == "complex"  # >8000 (score+3) + pattern hits (score+3) = 6 → complex
        # 8000+ / 2000 = 4+ splits, capped at 10
        assert a.suggested_split_count >= 4

    def test_very_large_capped_at_10(self, detector):
        text = _generate_words(20000)
        a = detector.analyze(_dict_req(text))
        assert a.suggested_split_count <= 10


# ---------------------------------------------------------------------------
# 7. test_multi_file_refactor - mentions multiple files, needs split by files
# ---------------------------------------------------------------------------


class TestMultiFileRefactor:
    def test_multi_file_detection(self, detector):
        text = (
            "Refactor all files in the project. Update router/main.py, "
            "router/session.py, router/routing.py, and router/fallback.py "
            "to use async/await patterns consistently."
        )
        a = detector.analyze(_dict_req(text))
        assert a.needs_splitting
        assert a.intent == "refactor"
        assert a.suggested_split_count >= 3  # at least 3 files detected

    def test_multi_file_triggers_split_via_file_count(self, detector):
        # Even short text with 2+ files triggers split
        text = "Update config.yaml and settings.json to match the new schema."
        a = detector.analyze(_dict_req(text))
        assert a.needs_splitting  # 2 files detected → split


# ---------------------------------------------------------------------------
# 8. test_single_file_analysis - mentions one file, may not split
# ---------------------------------------------------------------------------


class TestSingleFileAnalysis:
    def test_single_file_no_split(self, detector):
        text = "Analyze router/main.py for performance issues."
        a = detector.analyze(_dict_req(text))
        # One file alone shouldn't trigger a split (need >=2)
        # Short text, no complexity keywords
        assert not a.needs_splitting

    def test_single_file_with_high_complexity_may_split(self, detector):
        text = (
            "Analyze router/main.py for every module and all files "
            "in the entire codebase to find all performance issues."
        )
        a = detector.analyze(_dict_req(text))
        # "every module", "all files", "entire codebase" → complex → split
        assert a.needs_splitting
        assert a.complexity == "complex"


# ---------------------------------------------------------------------------
# 9. test_numbered_steps_3 - 3 numbered steps, may not split (exactly 3)
# ---------------------------------------------------------------------------


class TestNumberedSteps3:
    def test_three_numbered_steps(self, detector):
        text = (
            "1. Add auth middleware\n"
            "2. Create login endpoint\n"
            "3. Add rate limiting\n"
        )
        a = detector.analyze(_dict_req(text))
        # _should_split: >= 3 numbered steps triggers split
        assert a.needs_splitting
        assert a.suggested_split_count >= 3

    def test_three_step_words(self, detector):
        text = (
            "First, initialize the database.\n"
            "Second, create the schema.\n"
            "Third, seed the data.\n"
        )
        a = detector.analyze(_dict_req(text))
        # "First,", "Second,", "Third," match the numbered step regex
        assert a.needs_splitting


# ---------------------------------------------------------------------------
# 10. test_numbered_steps_10 - 10 numbered steps, needs split
# ---------------------------------------------------------------------------


class TestNumberedSteps10:
    def test_ten_numbered_steps(self, detector):
        steps = "\n".join(f"{i}. Step number {i} description text" for i in range(1, 11))
        a = detector.analyze(_dict_req(steps))
        assert a.needs_splitting
        assert a.suggested_split_count >= 3
        # Should suggest splitting by steps (capped at 10)
        assert a.suggested_split_count <= 10


# ---------------------------------------------------------------------------
# 11. test_intent_refactor - 'refactor all code' → intent=refactor
# ---------------------------------------------------------------------------


class TestIntentRefactor:
    @pytest.mark.parametrize("keyword", [
        "refactor", "rewrite", "restructure", "reorganize", "clean up", "modernize"
    ])
    def test_refactor_keywords(self, detector, keyword):
        a = detector.analyze(_dict_req(f"Please {keyword} the codebase."))
        assert a.intent == "refactor"


# ---------------------------------------------------------------------------
# 12. test_intent_analyze - 'analyze this log' → intent=analyze
# ---------------------------------------------------------------------------


class TestIntentAnalyze:
    @pytest.mark.parametrize("keyword", [
        "analyze", "review", "audit", "inspect", "explain", "diagnose", "find bugs"
    ])
    def test_analyze_keywords(self, detector, keyword):
        a = detector.analyze(_dict_req(f"Please {keyword} this module."))
        assert a.intent == "analyze"


# ---------------------------------------------------------------------------
# 13. test_intent_generate - 'generate a complete app' → intent=generate
# ---------------------------------------------------------------------------


class TestIntentGenerate:
    @pytest.mark.parametrize("keyword", [
        "generate", "create", "write", "build", "implement", "add", "develop"
    ])
    def test_generate_keywords(self, detector, keyword):
        a = detector.analyze(_dict_req(f"Please {keyword} a new feature."))
        assert a.intent == "generate"


# ---------------------------------------------------------------------------
# 14. test_intent_translate - 'translate to French' → intent=translate
# ---------------------------------------------------------------------------


class TestIntentTranslate:
    @pytest.mark.parametrize("keyword", [
        "translate", "convert", "port", "migrate", "transpile"
    ])
    def test_translate_keywords(self, detector, keyword):
        a = detector.analyze(_dict_req(f"Please {keyword} this to TypeScript."))
        assert a.intent == "translate"


# ---------------------------------------------------------------------------
# 14b. Additional intent tests
# ---------------------------------------------------------------------------


class TestIntentTestAndDocument:
    def test_intent_test(self, detector):
        # Note: "write" triggers "generate" before "test" in the ordered intent map.
        # Use phrasing that avoids earlier-match keywords.
        a = detector.analyze(_dict_req("Add unit test coverage for the API endpoints."))
        # "add" triggers generate first — use different phrasing
        a2 = detector.analyze(_dict_req("Test the API thoroughly with mocks."))
        assert a2.intent == "test"

    def test_intent_document(self, detector):
        # Note: "add" triggers "generate" before "document". Use "document" directly.
        a = detector.analyze(_dict_req("Document all public functions with docstrings."))
        assert a.intent == "document"

    def test_intent_general_fallback(self, detector):
        a = detector.analyze(_dict_req("Hello, how are you today?"))
        assert a.intent == "general"

    def test_intent_first_match_wins(self, detector):
        # "refactor" appears before "create" in the intent map → refactor wins
        a = detector.analyze(_dict_req("Refactor and create new tests."))
        assert a.intent == "refactor"


# ---------------------------------------------------------------------------
# 15. test_complexity_simple - short text → simple
# ---------------------------------------------------------------------------


class TestComplexitySimple:
    def test_short_text_simple(self, detector):
        a = detector.analyze(_dict_req("Fix the typo."))
        assert a.complexity == "simple"

    def test_no_keywords_short(self, detector):
        a = detector.analyze(_dict_req("What time is it?"))
        assert a.complexity == "simple"


# ---------------------------------------------------------------------------
# 16. test_complexity_medium - medium text with some keywords → medium
# ---------------------------------------------------------------------------


class TestComplexityMedium:
    def test_single_file_medium(self, detector):
        # One file hit → score += 1; with tokens >2000 → score += 1; total = 2 → medium
        text = _generate_words(1600) + " in router/main.py"
        a = detector.analyze(_dict_req(text))
        # tokens > 2000 → score += 1, file hit → score += 1 = 2 → medium
        assert a.complexity == "medium"

    def test_pattern_keyword_medium(self, detector):
        # "step by step" matches a complexity pattern → score += 1
        # Need another point from tokens or files
        text = _generate_words(1600) + " step by step"
        a = detector.analyze(_dict_req(text))
        assert a.complexity in ("medium", "complex")


# ---------------------------------------------------------------------------
# 17. test_complexity_complex - 'every module' 'all files' → complex
# ---------------------------------------------------------------------------


class TestComplexityComplex:
    def test_multi_file_keywords_complex(self, detector):
        text = (
            "Refactor every module and all files in the entire codebase. "
            "Update all classes across files to use the new pattern."
        )
        a = detector.analyze(_dict_req(text))
        assert a.complexity == "complex"
        assert a.needs_splitting

    def test_high_token_count_complex(self, detector):
        # 7000 unique words ≈ 9100 tokens (score +3), but need score ≥4 for "complex"
        # Add complexity keywords to push score to 4+
        text = " ".join(f"word{i:04d}" for i in range(7000))
        text += " every module all files entire codebase all classes"
        a = detector.analyze(_dict_req(text))
        assert a.complexity == "complex"

    def test_complex_forces_split(self, detector):
        # Need score >= 4 for "complex". Use 4+ complexity pattern keywords.
        text = "Update every module, all modules, all files, and all classes across files in the entire codebase."
        a = detector.analyze(_dict_req(text))
        assert a.complexity == "complex"
        assert a.needs_splitting


# ---------------------------------------------------------------------------
# 18. test_dict_input - pass dict instead of object, still works
# ---------------------------------------------------------------------------


class TestDictInput:
    def test_dict_request(self, detector):
        req = {"messages": [{"role": "user", "content": "Hello world"}]}
        a = detector.analyze(req)
        assert isinstance(a, TaskAnalysis)
        assert not a.needs_splitting
        assert a.complexity == "simple"

    def test_dict_with_multiple_messages(self, detector):
        req = {
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is Python?"},
            ]
        }
        a = detector.analyze(req)
        assert isinstance(a, TaskAnalysis)
        # Both messages should be concatenated
        assert a.estimated_tokens > 0

    def test_dict_empty_messages_key(self, detector):
        req = {"messages": None}
        a = detector.analyze(req)
        assert not a.needs_splitting

    def test_dict_missing_messages_key(self, detector):
        req = {}
        a = detector.analyze(req)
        assert not a.needs_splitting

    def test_object_request(self, detector):
        req = _obj_req("Hello world")
        a = detector.analyze(req)
        assert isinstance(a, TaskAnalysis)
        assert not a.needs_splitting

    def test_dict_and_object_produce_same_result(self, detector):
        text = "Analyze the performance of the database module."
        dict_req = _dict_req(text)
        obj_req = _obj_req(text)
        a_dict = detector.analyze(dict_req)
        a_obj = detector.analyze(obj_req)
        assert a_dict.needs_splitting == a_obj.needs_splitting
        assert a_dict.complexity == a_obj.complexity
        assert a_dict.intent == a_obj.intent
        assert a_dict.estimated_tokens == a_obj.estimated_tokens


# ---------------------------------------------------------------------------
# 19. test_mixed_content_with_code_blocks - text with code blocks
# ---------------------------------------------------------------------------


class TestMixedContentWithCodeBlocks:
    def test_code_block_in_text(self, detector):
        text = (
            "Fix this function:\n"
            "```python\n"
            "def hello():\n"
            "    print('hello')\n"
            "```\n"
            "It should return a value instead of printing."
        )
        a = detector.analyze(_dict_req(text))
        assert isinstance(a, TaskAnalysis)
        assert not a.needs_splitting
        assert a.intent == "general"  # no strong keyword match

    def test_large_code_block(self, detector):
        code_lines = "\n".join(f"    x_{i} = {i} * 2 + 1" for i in range(500))
        text = f"Refactor this code:\n```python\ndef big_func():\n{code_lines}\n```"
        a = detector.analyze(_dict_req(text))
        assert a.intent == "refactor"
        # Large code block may push tokens over threshold
        if a.estimated_tokens > TOKEN_SPLIT_THRESHOLD:
            assert a.needs_splitting

    def test_multi_part_content(self, detector):
        """Test multi-part content (vision-style messages)."""
        req = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Analyze this code structure."},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
                    ],
                }
            ]
        }
        a = detector.analyze(req)
        assert isinstance(a, TaskAnalysis)
        assert a.intent == "analyze"


# ---------------------------------------------------------------------------
# 20. test_chinese_text - non-English text token estimation
# ---------------------------------------------------------------------------


class TestChineseText:
    def test_chinese_text_basic(self, detector):
        text = "请分析这段代码的性能问题并提出优化建议。"
        a = detector.analyze(_dict_req(text))
        assert isinstance(a, TaskAnalysis)
        assert not a.needs_splitting
        assert a.estimated_tokens > 0

    def test_chinese_text_long(self, detector):
        # Without tiktoken, fallback uses word_count * 1.3. Chinese with no spaces = few words.
        # Add spaces to simulate word boundaries for the fallback estimator.
        text = "请分析 这段代码 的性能 问题 并 提出 优化 建议 包括 内存 使用 CPU 占用 响应时间 " * 200
        a = detector.analyze(_dict_req(text))
        # With spaces, fallback sees ~2600 words * 1.3 ≈ 3380 tokens
        # With tiktoken, Chinese chars tokenize to many tokens
        assert a.estimated_tokens > 100

    def test_japanese_text(self, detector):
        text = "このコードをリファクタリングしてください。"  # "Please refactor this code"
        a = detector.analyze(_dict_req(text))
        assert isinstance(a, TaskAnalysis)

    def test_mixed_language_text(self, detector):
        text = "Please refactor the 数据库模块 and update the API endpoints."
        a = detector.analyze(_dict_req(text))
        assert a.intent == "refactor"  # English keyword still detected
        assert a.estimated_tokens > 0


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


class TestTokenEstimation:
    def test_estimate_tokens_public_method(self, detector):
        count = detector.estimate_tokens("hello world")
        assert count > 0
        assert isinstance(count, int)

    def test_estimate_tokens_empty(self, detector):
        count = detector.estimate_tokens("")
        assert count == 0

    def test_estimate_tokens_scales(self, detector):
        short = detector.estimate_tokens("hello")
        long = detector.estimate_tokens("hello " * 100)
        assert long > short


class TestCustomThreshold:
    def test_lower_threshold(self):
        detector = TaskSizeDetector(token_threshold=100)
        text = _generate_words(200)  # ~260 tokens
        a = detector.analyze(_dict_req(text))
        assert a.needs_splitting

    def test_higher_threshold(self):
        detector = TaskSizeDetector(token_threshold=100000)
        text = _generate_words(500)
        a = detector.analyze(_dict_req(text))
        # With high threshold, only complexity or file/step rules trigger split
        if a.complexity != "complex":
            assert not a.needs_splitting


class TestExtractTextEdgeCases:
    def test_non_string_content_ignored(self, detector):
        """Non-string, non-list content should be silently skipped."""
        req = {"messages": [{"role": "user", "content": 42}]}
        text = detector._extract_text(req)
        assert text == ""

    def test_object_messages_with_none_content(self, detector):
        req = _Req([_Msg("user", None)])
        text = detector._extract_text(req)
        assert text == ""

    def test_multiple_messages_concatenated(self, detector):
        req = {"messages": [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "User message"},
        ]}
        text = detector._extract_text(req)
        assert "System prompt" in text
        assert "User message" in text

    def test_object_without_messages_attr(self, detector):
        """Object with no .messages should not crash."""
        class EmptyObj:
            pass
        a = detector.analyze(EmptyObj())
        assert not a.needs_splitting


class TestSuggestSplitCount:
    def test_split_by_files_count(self, detector):
        text = "Update router/main.py, router/session.py, router/routing.py"
        count = detector._suggest_split_count(text, 100, "simple")
        assert count == 3  # 3 files detected

    def test_split_by_steps_count(self, detector):
        text = "1. First\n2. Second\n3. Third\n4. Fourth\n5. Fifth"
        count = detector._suggest_split_count(text, 100, "simple")
        assert count >= 3

    def test_split_by_tokens_count(self, detector):
        count = detector._suggest_split_count("no files or steps here", 6000, "simple")
        # 6000 / 2000 = 3
        assert count == 3

    def test_split_count_max_10(self, detector):
        files = ", ".join(f"file{i}.py" for i in range(20))
        count = detector._suggest_split_count(files, 100, "simple")
        assert count <= 10

    def test_split_count_min_2_for_tokens(self, detector):
        count = detector._suggest_split_count("some text", 1000, "simple")
        # tokens > 0 → max(2, ...)
        assert count >= 2


class TestShouldSplitTriggers:
    def test_split_by_token_threshold(self, detector):
        assert detector._should_split("", 5000, "simple") is True

    def test_split_by_complexity(self, detector):
        assert detector._should_split("", 100, "complex") is True

    def test_split_by_multi_file(self, detector):
        text = "Update main.py and config.yaml"
        assert detector._should_split(text, 100, "simple") is True

    def test_no_split_simple(self, detector):
        assert detector._should_split("hello", 10, "simple") is False

    def test_split_by_numbered_steps(self, detector):
        text = "1. First\n2. Second\n3. Third"
        assert detector._should_split(text, 100, "simple") is True
