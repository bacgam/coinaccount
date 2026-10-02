from __future__ import annotations

from typing import Protocol


class SettlementClient(Protocol):
    def get_settlements(
        self, *, limit: int, offset: int, sort_direction: str
    ) -> list[dict[str, object]]: ...


def download_all_settlements(
    client: SettlementClient, *, page_size: int = 1_000, max_pages: int = 1_000
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for page in range(max_pages):
        batch = client.get_settlements(
            limit=page_size,
            offset=page * page_size,
            sort_direction="Asc",
        )
        records.extend(batch)
        if len(batch) < page_size:
            return records
    raise RuntimeError("정산 내역이 안전 페이지 한도를 초과했습니다.")
