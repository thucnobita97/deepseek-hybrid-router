"""Shared Pydantic models for the DeepSeek Hybrid Router."""
from __future__ import annotations

from pydantic import BaseModel
from typing import List, Optional, Dict, Any, Union


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: Dict[str, Any]


class ToolDefinition(BaseModel):
    type: str = "function"
    function: Dict[str, Any]


class Message(BaseModel):
    role: str
    content: Optional[Union[str, List[Dict[str, Any]]]] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    temperature: Optional[float] = 1.0
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    tools: Optional[List[ToolDefinition]] = None
    functions: Optional[List[Dict[str, Any]]] = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Dict[str, Any]]
    usage: Dict[str, int]
