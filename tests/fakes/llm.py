from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.providers.llm import LLMProvider, LLMResponse, Message, ToolDefinition


@dataclass
class FakeLLMProvider:
    """Respuestas encoladas por el test; registra cada llamada."""

    structured_responses: list[BaseModel] = field(default_factory=list[BaseModel])
    chat_responses: list[LLMResponse] = field(default_factory=list[LLMResponse])
    calls: list[tuple[str, tuple[Message, ...]]] = field(
        default_factory=list[tuple[str, tuple[Message, ...]]]
    )

    async def generate_structured[T: BaseModel](
        self, messages: Sequence[Message], schema: type[T]
    ) -> T:
        self.calls.append(("generate_structured", tuple(messages)))
        if not self.structured_responses:
            raise AssertionError("FakeLLMProvider: no hay respuesta estructurada encolada")
        response = self.structured_responses.pop(0)
        if not isinstance(response, schema):
            raise AssertionError(f"FakeLLMProvider: se esperaba {schema}, hay {type(response)}")
        return response

    async def chat(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition] = ()
    ) -> LLMResponse:
        self.calls.append(("chat", tuple(messages)))
        if not self.chat_responses:
            raise AssertionError("FakeLLMProvider: no hay respuesta de chat encolada")
        return self.chat_responses.pop(0)


_: type[LLMProvider] = FakeLLMProvider
