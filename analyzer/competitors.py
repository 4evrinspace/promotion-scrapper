"""Блок В (ч.3): сравнение цен между конкурентами.

Связь товаров между магазинами даёт canonical_product_id из нормализатора.
На текущем датасете пересечений нет ни одного (все матчи provisional,
ни один товар не встречается в двух магазинах), поэтому кроме товарного
сравнения считается сравнение на уровне товарных групп — оно не требует
матчинга и даёт содержательный результат уже сейчас.
"""

import pandas as pd  # pyright: ignore[reportMissingModuleSource]
from typing import Any, cast

from analyzer.data_prep import weighted_median

TRUSTED_MATCH = ("confirmed", "auto_matched")


def _per_shop_prices(states: pd.DataFrame) -> pd.DataFrame:
    """Цена товара в магазине = медиана промо-цены, взвешенная по времени."""
    rows = []
    grouped = cast(Any, states.groupby(["canonical_product_id", "shop"]))
    for (pid, shop), g in grouped:
        rows.append({
            "canonical_product_id": pid,
            "shop": shop,
            "name": g["name"].iloc[0],
            "category": g["category"].iloc[0],
            "group": g["group"].iloc[0],
            "promo_price": weighted_median(g["promotion_price"], g["weight_hours"]),
            "discount_pct": weighted_median(g["discount_pct"], g["weight_hours"]),
            "match_status": g["match_status"].mode().iat[0],
        })
    return pd.DataFrame(rows)


def competitor_comparison(states: pd.DataFrame, trusted_only: bool = False) -> pd.DataFrame:
    """Товары, представленные минимум в двух магазинах: разброс цен.

    dispersion_pct — насколько максимальная цена дороже минимальной;
    provisional_share — доля низкоуверенного матчинга. Сравнение на
    provisional-матчах показывается, но помечается как ненадёжное:
    строго говоря, до подтверждения матча это могут быть разные товары.
    """
    per_shop = _per_shop_prices(states)
    if trusted_only:
        per_shop = per_shop[per_shop["match_status"].isin(TRUSTED_MATCH)]

    counts = per_shop.groupby("canonical_product_id")["shop"].transform("nunique")
    present = per_shop[counts >= 2]

    rows = []
    for pid, g in present.groupby("canonical_product_id"):
        prices = g.set_index("shop")["promo_price"]
        rows.append({
            "canonical_product_id": pid,
            "name": g["name"].iloc[0],
            "category": g["category"].iloc[0],
            "group": g["group"].iloc[0],
            "n_shops": int(len(g)),
            "min_price": prices.min(),
            "median_price": prices.median(),
            "max_price": prices.max(),
            "dispersion_pct": round((prices.max() - prices.min()) / prices.min() * 100, 2),
            "savings_pct": round((prices.max() - prices.min()) / prices.max() * 100, 2),
            "best_shop": prices.idxmin(),
            "worst_shop": prices.idxmax(),
            "provisional_share": round((g["match_status"] == "provisional").mean(), 2),
            "reliable": bool((g["match_status"] != "provisional").all()),
        })

    result = pd.DataFrame(rows)
    if result.empty:
        return result
    return result.sort_values("dispersion_pct", ascending=False).reset_index(drop=True)


def shop_price_index(states: pd.DataFrame) -> pd.DataFrame:
    """Индекс цен магазина по сопоставимым товарам (1.0 = всегда самый дешёвый).

    Считается только по товарам, которые есть минимум в двух магазинах.
    Без этого фильтра каждый магазин оказывается единственным продавцом
    своего товара, индекс у всех равен ровно 1.0 и таблица бессмысленна.
    """
    per_shop = _per_shop_prices(states)
    counts = per_shop.groupby("canonical_product_id")["shop"].transform("nunique")
    comparable = per_shop[counts >= 2].copy()
    if comparable.empty:
        return pd.DataFrame(
            columns=["shop", "avg_price_index", "n_comparable_products", "cheapest_share"]
        )

    comparable["min_price"] = comparable.groupby("canonical_product_id")[
        "promo_price"
    ].transform("min")
    comparable["price_index"] = comparable["promo_price"] / comparable["min_price"]
    comparable["is_cheapest"] = comparable["price_index"] <= 1.0

    summary = cast(
        pd.DataFrame,
        comparable.groupby("shop", as_index=False).agg(
            avg_price_index=("price_index", "mean"),
            n_comparable_products=("canonical_product_id", "nunique"),
            cheapest_share=("is_cheapest", "mean"),
        ),
    )
    return (
        summary
        .sort_values(by=["avg_price_index"])
        .round(3)
        .reset_index(drop=True)
    )


def group_price_comparison(states: pd.DataFrame) -> pd.DataFrame:
    """Сравнение магазинов на уровне товарных групп (не требует матчинга).

    Отвечает на вопрос «кто агрессивнее по скидкам в этой категории»,
    даже когда одинаковых товаров в выдаче нет. Цены между магазинами
    сравниваются осторожно: внутри группы ассортимент разный, поэтому
    основной показатель — глубина скидки, а не абсолютная цена.
    """
    per_shop = _per_shop_prices(states)
    table = (
        per_shop.groupby(["category", "group", "shop"], as_index=False)
        .agg(
            products=("canonical_product_id", "nunique"),
            median_discount=("discount_pct", "median"),
            max_discount=("discount_pct", "max"),
            median_price=("promo_price", "median"),
        )
    )
    shops_per_group = table.groupby("group")["shop"].transform("nunique")
    table["comparable"] = shops_per_group >= 2
    table["discount_rank_in_group"] = table.groupby("group")["median_discount"].rank(
        ascending=False, method="min"
    )
    return table.sort_values(
        ["comparable", "group", "median_discount"], ascending=[False, True, False]
    ).reset_index(drop=True).round(2)
