"""Блок А: история цен и глубина скидки."""

from typing import cast

import pandas as pd  # pyright: ignore[reportMissingModuleSource]

from analyzer.data_prep import weighted_mean, weighted_median


def price_history(
    features: pd.DataFrame,
    product_id: str | None = None,
    shop: str | None = None,
    category: str | None = None,
    group: str | None = None,
) -> pd.DataFrame:
    """Возвращает таймсерию цен по фильтрам (магазин/категория/группа/товар)."""
    mask = pd.Series(True, index=features.index)
    if product_id is not None:
        mask &= features["canonical_product_id"] == product_id
    if shop is not None:
        mask &= features["shop"] == shop
    if category is not None:
        mask &= features["category"] == category
    if group is not None:
        mask &= features["group"] == group

    cols = [
        "canonical_product_id", "name", "shop", "category", "group", "date",
        "original_price", "promotion_price", "discount_pct", "match_status",
    ]
    selected = cast(pd.DataFrame, features.loc[mask, cols])
    return selected.sort_values(["canonical_product_id", "date"])


def price_timeline(states: pd.DataFrame) -> pd.DataFrame:
    """Компактная история цен: по строке на интервал постоянной цены.

    Для графиков и для защиты кейса это нагляднее сырых снимков: 500
    наблюдений превращаются в десятки строк с датами изменения цены.
    """
    cols = [
        "canonical_product_id", "name", "shop", "category", "group", "episode_id",
        "first_seen", "last_seen", "duration_hours", "n_obs",
        "original_price", "promotion_price", "discount_pct",
    ]
    out = cast(pd.DataFrame, states[cols].copy())
    out["saving"] = (out["original_price"] - out["promotion_price"]).round(2)
    out["discount_pct"] = out["discount_pct"].round(2)
    out["duration_hours"] = out["duration_hours"].round(2)
    return out.sort_values(
        ["shop", "canonical_product_id", "first_seen"],
    ).reset_index(drop=True)


def discount_stats(states: pd.DataFrame, by: str = "shop") -> pd.DataFrame:
    """Распределение глубины скидки в разрезе магазина/категории/группы.

    Считается по состояниям цен и взвешивается по времени их жизни.
    По сырым наблюдениям считать нельзя: парсер пишет одну и ту же цену
    каждые 10 минут, поэтому «среднее по строкам» — это среднее по частоте
    опроса, а товар, дольше провисевший в выдаче, незаслуженно перевешивает.
    """
    allowed = {"shop", "category", "group"}
    if by not in allowed:
        raise ValueError(f"by должен быть одним из {sorted(allowed)}")

    rows = []
    for key, g in states.groupby(by):
        # на уровне товара берём взвешенную по времени медиану скидки,
        # чтобы товар с 20 состояниями не перевесил товар с одним
        per_product_rows = []
        for key, product_states in g.groupby(
            ["canonical_product_id", "shop"]
        ):
            product_id, product_shop = cast(tuple[str, str], key)
            per_product_rows.append({
                "canonical_product_id": product_id,
                "shop": product_shop,
                "discount": weighted_median(
                    product_states["discount_pct"], product_states["weight_hours"]
                ),
                "price": weighted_median(
                    product_states["promotion_price"], product_states["weight_hours"]
                ),
                "hours": product_states["weight_hours"].sum(),
            })
        per_product = pd.DataFrame(per_product_rows)
        discounts = per_product["discount"]
        rows.append({
            by: key,
            "products": int(per_product["canonical_product_id"].nunique()),
            "price_states": int(len(g)),
            "observations": int(g["n_obs"].sum()),
            "discount_mean": weighted_mean(discounts, per_product["hours"]),
            "discount_median": discounts.median(),
            "discount_p25": discounts.quantile(0.25),
            "discount_p75": discounts.quantile(0.75),
            "discount_p95": discounts.quantile(0.95),
            "discount_max": discounts.max(),
            "price_median": per_product["price"].median(),
        })

    return (
        pd.DataFrame(rows)
        .sort_values("discount_median", ascending=False)
        .reset_index(drop=True)
        .round(2)
    )


def promo_episodes(states: pd.DataFrame) -> pd.DataFrame:
    """Эпизоды акций: непрерывное присутствие товара в промо-выдаче.

    Эпизод может содержать несколько состояний цен — именно смена цены
    внутри эпизода и есть материал для блока лже-акций.
    """
    episodes = (
        states.groupby(["canonical_product_id", "shop", "episode_id"], as_index=False)
        .agg(
            name=("name", "first"),
            category=("category", "first"),
            group=("group", "first"),
            n_price_states=("first_seen", "size"),
            started_at=("first_seen", "min"),
            ended_at=("last_seen", "max"),
            median_discount=("discount_pct", "median"),
            max_discount=("discount_pct", "max"),
            min_price=("promotion_price", "min"),
            max_price=("promotion_price", "max"),
        )
    )
    episodes["duration_hours"] = (
        (episodes["ended_at"] - episodes["started_at"]).dt.total_seconds() / 3600
    ).round(2)
    episodes["duration_days"] = (episodes["duration_hours"] / 24).round(2)
    episodes["price_changed"] = episodes["n_price_states"] > 1
    return episodes.sort_values(["shop", "started_at"]).reset_index(drop=True)


def product_promo_summary(states: pd.DataFrame) -> pd.DataFrame:
    """Свод по товару за всё окно наблюдения."""
    episodes = promo_episodes(states)
    summary = (
        episodes.groupby(["canonical_product_id", "shop"], as_index=False)
        .agg(
            name=("name", "first"),
            category=("category", "first"),
            group=("group", "first"),
            n_episodes=("episode_id", "nunique"),
            n_price_states=("n_price_states", "sum"),
            first_seen=("started_at", "min"),
            last_seen=("ended_at", "max"),
            promo_hours=("duration_hours", "sum"),
            median_discount=("median_discount", "median"),
            max_discount=("max_discount", "max"),
            min_price=("min_price", "min"),
            max_price=("max_price", "max"),
        )
    )
    summary["observed_hours"] = (
        (summary["last_seen"] - summary["first_seen"]).dt.total_seconds() / 3600
    ).round(2)
    summary["promo_days"] = (summary["promo_hours"] / 24).round(2)
    summary["price_spread_pct"] = (
        (summary["max_price"] / summary["min_price"] - 1) * 100
    ).round(2)
    numeric = summary.select_dtypes("number").columns
    summary[numeric] = summary[numeric].round(2)
    return summary
