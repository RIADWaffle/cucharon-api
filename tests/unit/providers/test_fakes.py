from decimal import Decimal

import pytest
from pydantic import BaseModel

from app.providers.food_data import Nutrients, ProductData
from app.providers.llm import LLMResponse, Message
from app.providers.storage import InvalidStorageKeyError
from tests.fakes.food_data import FakeFoodDataProvider
from tests.fakes.llm import FakeLLMProvider
from tests.fakes.storage import FakeStorageProvider


class Item(BaseModel):
    name: str


class Other(BaseModel):
    x: int


async def test_fake_llm_returns_queued_structured_response() -> None:
    llm = FakeLLMProvider(structured_responses=[Item(name="huevo")])
    result = await llm.generate_structured([Message(role="user", content="2 huevos")], Item)
    assert result == Item(name="huevo")
    assert llm.calls[0][0] == "generate_structured"


async def test_fake_llm_fails_loudly_without_queue_or_wrong_schema() -> None:
    llm = FakeLLMProvider()
    with pytest.raises(AssertionError):
        await llm.generate_structured([], Item)
    with pytest.raises(AssertionError):
        await llm.chat([])
    llm.structured_responses.append(Other(x=1))
    with pytest.raises(AssertionError):
        await llm.generate_structured([], Item)


async def test_fake_llm_chat() -> None:
    reply = LLMResponse(message=Message(role="assistant", content="hola"), finish_reason="stop")
    llm = FakeLLMProvider(chat_responses=[reply])
    assert await llm.chat([Message(role="user", content="hola")]) is reply


async def test_fake_food_data() -> None:
    avena = ProductData(
        source="off",
        external_id="1",
        name="Avena integral",
        barcode="750100",
        nutrients_per_100g=Nutrients(kcal=Decimal(389)),
    )
    food = FakeFoodDataProvider(products=[avena])
    assert await food.get_by_barcode("750100") is avena
    assert await food.get_by_barcode("000") is None
    assert await food.search("AVENA") == [avena]
    assert avena.nutrition_complete
    assert not ProductData(
        source="usda", external_id="2", name="x", nutrients_per_100g=Nutrients()
    ).nutrition_complete


async def test_fake_storage() -> None:
    storage = FakeStorageProvider()
    upload = await storage.presign_upload("users/u/a.jpg", "image/jpeg", 10)
    download = await storage.presign_download("users/u/a.jpg")
    await storage.delete("users/u/a.jpg")
    assert upload.url.endswith("/upload/users/u/a.jpg")
    assert download.url.endswith("/download/users/u/a.jpg")
    assert storage.uploads == [("users/u/a.jpg", "image/jpeg", 10)]
    assert storage.deleted == ["users/u/a.jpg"]
    with pytest.raises(InvalidStorageKeyError):
        await storage.delete("../x")
