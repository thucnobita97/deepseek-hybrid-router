"""Tests for request analyzer."""
import pytest
from router.models import ChatCompletionRequest, Message, ToolCall, ToolDefinition
from router.analyzer import RequestAnalyzer, RequestType


def test_tool_calling_with_tools_definition():
    """Request with tools array should be classified as TOOL_CALLING."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="What's the weather?")],
        tools=[
            ToolDefinition(
                type="function",
                function={
                    "name": "get_weather",
                    "description": "Get current weather",
                    "parameters": {"type": "object", "properties": {}}
                }
            )
        ]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.TOOL_CALLING
    assert result.confidence == 1.0
    assert result.metadata["has_tools_definition"] is True


def test_tool_calling_with_functions_definition():
    """Request with functions array should be classified as TOOL_CALLING."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="Calculate 2+2")],
        functions=[{"name": "calculator", "description": "Do math"}]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.TOOL_CALLING
    assert result.confidence == 1.0


def test_tool_calling_with_tool_role():
    """Message with role='tool' should be classified as TOOL_CALLING."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[
            Message(role="user", content="What's the weather?"),
            Message(role="tool", content="Sunny, 72°F")
        ]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.TOOL_CALLING
    assert result.metadata["has_tool_role"] is True


def test_tool_calling_with_tool_calls():
    """Message with tool_calls should be classified as TOOL_CALLING."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[
            Message(role="user", content="What's the weather?"),
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_123",
                        type="function",
                        function={"name": "get_weather", "arguments": "{}"}
                    )
                ]
            )
        ]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.TOOL_CALLING
    assert result.metadata["has_tool_calls"] is True


def test_vision_with_image_url():
    """Message with image_url content should be classified as VISION."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[
            Message(
                role="user",
                content=[
                    {"type": "text", "text": "What's in this image?"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}}
                ]
            )
        ]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.VISION
    assert result.confidence == 1.0
    assert result.metadata["image_block_count"] == 1
    assert "image_url" in result.metadata["image_block_types"]


def test_vision_with_image_type():
    """Message with image type should be classified as VISION."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[
            Message(
                role="user",
                content=[
                    {"type": "text", "text": "Describe this"},
                    {"type": "image", "image": "base64data"}
                ]
            )
        ]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.VISION
    assert "image" in result.metadata["image_block_types"]


def test_reasoning_with_keywords():
    """Request with reasoning keywords should be classified as REASONING."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[
            Message(
                role="user",
                content="Explain why the sky is blue. Prove your answer with math and derive the equations."
            )
        ]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.REASONING
    assert result.metadata["score"] >= 3
    assert "explain why" in result.metadata["matched_keywords"]


def test_reasoning_with_long_message():
    """Long user message (>500 chars) contributes to REASONING score."""
    long_text = "Please analyze this complex problem. " + "x" * 550
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content=long_text)]
    )
    result = RequestAnalyzer().analyze(request)
    # Should have: "analyze" (1) + long message (1) = 2, not enough for threshold 3
    # So this should fall through to CHAT unless we add more signals
    # Let's make it actually trigger reasoning:
    long_text = "Please analyze this complex problem and think through the architecture. " + "x" * 550
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content=long_text)]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.REASONING
    assert result.metadata["longest_user_message_chars"] > 500


def test_reasoning_with_multiple_questions():
    """Multiple question marks contribute to REASONING score."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[
            Message(
                role="user",
                content="Can you analyze this? What do you think? How would you calculate it?"
            )
        ]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.REASONING
    assert result.metadata["question_mark_count"] >= 2


def test_search_with_keywords():
    """Request with search keywords should be classified as SEARCH."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="What's the latest news about AI?")]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.SEARCH
    assert "latest" in result.metadata["matched_keywords"]


def test_search_with_current_events():
    """Request asking about current events should be classified as SEARCH."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="What happened yesterday in the stock market?")]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.SEARCH
    assert result.confidence > 0.5


def test_chat_default():
    """Simple request with no special signals should be classified as CHAT."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="Hello!")]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.CHAT
    assert result.confidence == 1.0
    assert result.metadata["message_count"] == 1


def test_priority_tool_over_vision():
    """TOOL_CALLING should take priority over VISION."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[
            Message(
                role="user",
                content=[
                    {"type": "text", "text": "Analyze this image"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/img.jpg"}}
                ]
            )
        ],
        tools=[
            ToolDefinition(
                type="function",
                function={"name": "analyze_image", "parameters": {}}
            )
        ]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.TOOL_CALLING


def test_priority_vision_over_reasoning():
    """VISION should take priority over REASONING."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[
            Message(
                role="user",
                content=[
                    {"type": "text", "text": "Explain why this image shows a cat and analyze it step by step"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/cat.jpg"}}
                ]
            )
        ]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.VISION


def test_classify_class_method():
    """The classify class method should work the same as analyze."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="Hello")]
    )
    result = RequestAnalyzer.classify(request)
    assert result.request_type == RequestType.CHAT


def test_custom_reasoning_threshold():
    """RequestAnalyzer should accept custom reasoning threshold."""
    # Lower threshold makes it easier to trigger REASONING
    analyzer = RequestAnalyzer(reasoning_threshold=1)
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="Can you analyze this?")]
    )
    result = analyzer.analyze(request)
    assert result.request_type == RequestType.REASONING
    
    # Higher threshold makes it harder
    analyzer_strict = RequestAnalyzer(reasoning_threshold=10)
    result_strict = analyzer_strict.analyze(request)
    assert result_strict.request_type != RequestType.REASONING


def test_multimodal_message_extraction():
    """Analyzer should extract text from multimodal messages correctly."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[
            Message(
                role="user",
                content=[
                    {"type": "text", "text": "What's the latest news?"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/news.jpg"}}
                ]
            )
        ]
    )
    # Should detect VISION first (higher priority), but text extraction should work
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.VISION


def test_empty_messages():
    """Request with empty content should default to CHAT."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="")]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.CHAT


def test_none_content():
    """Message with None content should not crash and default to CHAT."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="assistant", content=None)]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.CHAT


def test_multiple_tool_signals():
    """Request with multiple tool signals should report all in metadata."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[
            Message(role="user", content="Call a function"),
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(id="1", type="function", function={"name": "test", "arguments": "{}"})
                ]
            ),
            Message(role="tool", content="result")
        ],
        tools=[ToolDefinition(type="function", function={"name": "test", "parameters": {}})]
    )
    result = RequestAnalyzer().analyze(request)
    assert result.request_type == RequestType.TOOL_CALLING
    assert result.metadata["has_tools_definition"] is True
    assert result.metadata["has_tool_role"] is True
    assert result.metadata["has_tool_calls"] is True
