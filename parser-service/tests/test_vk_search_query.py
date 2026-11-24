import pytest

from app.adapters.vk import VkParserAdapter


class _FakeSearchInput:
    """Минимальный input[type=search]: type() дописывает символы, fill() заменяет значение."""

    def __init__(self, type_works: bool = True, fill_works: bool = True) -> None:
        self.value = ""
        self.type_works = type_works
        self.fill_works = fill_works
        self.calls: list[tuple[str, str]] = []

    def click(self) -> None:
        self.calls.append(("click", ""))

    def fill(self, value: str) -> None:
        self.calls.append(("fill", value))
        if self.fill_works:
            self.value = value

    def type(self, value: str, delay: float | None = None) -> None:
        self.calls.append(("type", value))
        if self.type_works:
            self.value += value

    def input_value(self) -> str:
        return self.value


def test_search_query_is_typed_and_verified():
    adapter = VkParserAdapter(storage_state={})
    search_input = _FakeSearchInput()

    adapter._enter_search_query(page=None, search_input=search_input, keyword="Строительство домов")

    assert search_input.value == "Строительство домов"
    assert [name for name, _ in search_input.calls] == ["click", "fill", "type"]


def test_search_query_falls_back_to_fill_when_type_does_nothing():
    """VK может отдать скрытый/не тот input: type() не меняет значение — должен выручить fill()."""
    adapter = VkParserAdapter(storage_state={})
    search_input = _FakeSearchInput(type_works=False)

    adapter._enter_search_query(page=None, search_input=search_input, keyword="Строительство домов")

    assert search_input.value == "Строительство домов"
    assert ("fill", "Строительство домов") in search_input.calls


def test_search_query_raises_when_query_did_not_reach_the_input():
    adapter = VkParserAdapter(storage_state={})
    search_input = _FakeSearchInput(type_works=False, fill_works=False)

    with pytest.raises(RuntimeError, match="не попал в строку поиска"):
        adapter._enter_search_query(page=None, search_input=search_input, keyword="Строительство домов")
