from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ProductSize:
    nm_id: int
    chrt_id: int
    vendor_code: str
    title: str
    size_name: str


@dataclass(frozen=True)
class Product:
    nm_id: int
    vendor_code: str
    title: str
    chrt_ids: tuple[int, ...]


@dataclass(frozen=True)
class SellerWarehouse:
    id: int
    name: str


@dataclass(frozen=True)
class FBSOrder:
    id: int
    article: str
    nm_id: int
    chrt_id: int
    warehouse_id: int
    office_id: int
    created_at: str
    cargo_type: int
    cross_border_type: int
    offices: tuple[str, ...] = ()


@dataclass(frozen=True)
class FBSSupply:
    id: str
    name: str
    done: bool
    cargo_type: int
    cross_border_type: int
    created_at: str = ""
    destination_office_id: int | None = None


def build_products(sizes: Iterable[ProductSize]) -> dict[int, Product]:
    grouped: dict[int, dict] = {}
    for size in sizes:
        row = grouped.setdefault(
            size.nm_id,
            {
                "vendor_code": size.vendor_code,
                "title": size.title,
                "chrt_ids": [],
            },
        )
        row["chrt_ids"].append(size.chrt_id)

    return {
        nm_id: Product(
            nm_id=nm_id,
            vendor_code=row["vendor_code"],
            title=row["title"],
            chrt_ids=tuple(dict.fromkeys(row["chrt_ids"])),
        )
        for nm_id, row in grouped.items()
    }


def aggregate_by_nm(
    chrt_quantities: dict[int, int],
    sizes: Iterable[ProductSize],
) -> dict[int, int]:
    result: dict[int, int] = {}
    for size in sizes:
        result[size.nm_id] = result.get(size.nm_id, 0) + int(
            chrt_quantities.get(size.chrt_id, 0)
        )
    return result
