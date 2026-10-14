from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "tool_calls", "length", "other"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    # Solo para role="tool": a qué ToolCall responde.
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    # JSON Schema de los argumentos.
    parameters: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class LLMResponse:
    message: Message
    finish_reason: FinishReason


class LLMProvider(Protocol):
    """El LLM solo parsea texto a estructuras y redacta. Nunca calcula números."""

    async def generate_structured[T: BaseModel](
        self, messages: Sequence[Message], schema: type[T]
    ) -> T: ...

    async def chat(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition] = ()
    ) -> LLMResponse: ...
