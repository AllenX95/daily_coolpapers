from collections.abc import Iterator


def chunks(items: list[int], size: int = 500) -> Iterator[list[int]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]
