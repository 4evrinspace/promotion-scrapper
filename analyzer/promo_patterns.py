"""Блок В (ч.1): «любимые» промо-товары.

Все товары в датасете акционные, поэтому «любимость» нельзя измерить как
долю дней в акции — знаменателя (дней без акции) у нас нет. Измеряем то,
что действительно наблюдаемо: как долго товар держится в промо-выдаче,
сколько раз магазин пересматривал цену во время акции, сколько раз
акция перезапускалась и насколько глубокая скидка.

Ранг считается внутри магазина: сравнивать длительность промо у «Ленты»
и «Холодильник.ру» напрямую нельзя — у них разная частота обновления
выдачи и разный размер каталога.
"""

import pandas as pd  # pyright: ignore[reportMissingModuleSource]

from analyzer.price_history import product_promo_summary


def _normalize(series: pd.Series) -> pd.Series:
    """Мин-макс нормировка; если все значения равны — возвращаем 0.5."""
    lo, hi = series.min(), series.max()
    if pd.isna(lo) or hi == lo:
        return pd.Series(0.5, index=series.index)
    return (series - lo) / (hi - lo)


def favorite_products(
    states: pd.DataFrame, top_n: int = 20, within_shop: bool = True
) -> pd.DataFrame:
    """Ранжирует товары по промо-активности магазина."""
    summary = product_promo_summary(states)
    if summary.empty:
        return summary

    summary["promo_time_share"] = (
        summary["promo_hours"] / summary["observed_hours"].replace(0, pd.NA)
    ).fillna(1.0)

    def score(block: pd.DataFrame) -> pd.Series:
        return (
            0.35 * _normalize(block["promo_hours"])
            + 0.25 * _normalize(block["n_episodes"].astype(float))
            + 0.20 * _normalize(block["n_price_states"].astype(float))
            + 0.20 * _normalize(block["median_discount"])
        )

    if within_shop:
        grouped = summary.groupby("shop")

        def grouped_normalize(column: str) -> pd.Series:
            values = summary[column].astype(float)
            minimum = grouped[column].transform("min")
            maximum = grouped[column].transform("max")
            return ((values - minimum) / (maximum - minimum)).where(
                maximum != minimum, 0.5
            )

        summary["score"] = (
            0.35 * grouped_normalize("promo_hours")
            + 0.25 * grouped_normalize("n_episodes")
            + 0.20 * grouped_normalize("n_price_states")
            + 0.20 * grouped_normalize("median_discount")
        ).round(3)
        summary["rank_in_shop"] = summary.groupby("shop")["score"].rank(
            ascending=False, method="min"
        )
    else:
        summary["score"] = score(summary).round(3)
        summary["rank_in_shop"] = summary["score"].rank(ascending=False, method="min")

    cols = [
        "canonical_product_id", "shop", "name", "category", "group", "score",
        "rank_in_shop", "n_episodes", "n_price_states", "promo_days",
        "promo_time_share", "median_discount", "max_discount",
        "min_price", "max_price", "price_spread_pct",
    ]
    return (
        summary.sort_values(["score"], ascending=False)
        .head(top_n)
        .filter(items=cols)
        .reset_index(drop=True)
        .round(3)
    )


def group_promo_activity(states: pd.DataFrame) -> pd.DataFrame:
    """Какие товарные группы магазин чаще всего держит в акции."""
    summary = product_promo_summary(states)
    table = (
        summary.groupby(["shop", "category", "group"], as_index=False)
        .agg(
            products=("canonical_product_id", "nunique"),
            episodes=("n_episodes", "sum"),
            promo_days=("promo_days", "sum"),
            median_discount=("median_discount", "median"),
        )
    )
    table["share_of_shop_products"] = table["products"] / table.groupby("shop")[
        "products"
    ].transform("sum")
    return table.sort_values(
        ["shop", "products"], ascending=[True, False]
    ).reset_index(drop=True).round(3)
