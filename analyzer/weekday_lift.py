"""Блок В (ч.2): промо-активность по дням недели.

ВАЖНО, две поправки к наивной версии:

1. Нельзя считать долю по числу наблюдений. Число строк в дне — это
   характеристика парсера (сколько часов он работал и как часто опрашивал),
   а не магазина. В контрольном прогоне на двухчасовом срезе наивная метрика
   давала lift ≈ 6.9 для четверга просто потому, что других дней в данных нет.
   Поэтому знаменатель — количество РЕАЛЬНО отнаблюдённых дат этого дня недели,
   а числитель — активность в пересчёте на один такой день.

2. Дни внутри одной акции автокоррелированы: акция на 3 дня даёт 3 «промо-дня»,
   но это одно решение магазина. Поэтому основная метрика — СТАРТЫ акций
   по дням недели (по эпизодам), а доля активных товаров идёт справочно.

Сглаживание Лапласа оставлено: при 4 днях данных оценка без него улетает.
"""

import pandas as pd  # pyright: ignore[reportMissingModuleSource]

from analyzer.config import WEEKDAY_NAMES


def _observed_weekdays(features: pd.DataFrame, by_shop: bool = True) -> pd.DataFrame:
    """Сколько календарных дат каждого дня недели реально попало в окно."""
    keys = ["shop", "weekday"] if by_shop else ["weekday"]
    days = features[keys + ["day"]].drop_duplicates()
    return (
        days.groupby(keys)["day"]
        .nunique()
        .rename("days_observed")
        .reset_index()
    )


def weekday_lift(
    features: pd.DataFrame, episodes: pd.DataFrame | None = None, alpha: float = 1.0
) -> pd.DataFrame:
    """Промо-активность по дням недели для каждого магазина и суммарно (ALL).

    starts_per_day   — сколько акций стартует в среднем за один такой день недели;
    active_per_day   — сколько товаров в среднем находится в промо в такой день;
    lift             — доля стартов в этот день / (1/7), со сглаживанием Лапласа;
    days_observed    — сколько таких дней реально отнаблюдено (0 = данных нет);
    reliable         — есть ли хотя бы 2 наблюдения этого дня недели.
    """
    frames = []
    for by_shop in (True, False):
        keys = ["shop", "weekday"] if by_shop else ["weekday"]
        observed = _observed_weekdays(features, by_shop)

        active = (
            features.assign(_d=features["day"])
            .groupby(keys + ["_d"], as_index=False)
            .agg(products=("canonical_product_id", "nunique"))
            .groupby(keys, as_index=False)
            .agg(active_per_day=("products", "mean"))
        )

        if episodes is not None and not episodes.empty:
            starts = episodes.copy()
            starts["weekday"] = starts["started_at"].dt.dayofweek
            starts["day"] = starts["started_at"].dt.date
            group_keys = ["shop", "weekday"] if by_shop else ["weekday"]
            starts = starts.groupby(group_keys, as_index=False).agg(
                promo_starts=("episode_id", "size")
            )
        else:
            starts = pd.DataFrame(columns=keys + ["promo_starts"])

        table = observed.merge(active, on=keys, how="left").merge(starts, on=keys, how="left")
        table["promo_starts"] = table["promo_starts"].fillna(0)

        # добиваем недостающие дни недели нулями: их отсутствие — тоже результат
        shops = table["shop"].unique() if by_shop else ["ALL"]
        full = pd.MultiIndex.from_product(
            [shops, range(7)], names=["shop", "weekday"]
        ).to_frame(index=False)
        if not by_shop:
            table = table.assign(shop="ALL")
        table = full.merge(table, on=["shop", "weekday"], how="left").fillna(
            {"days_observed": 0, "promo_starts": 0, "active_per_day": 0}
        )

        total_starts = table.groupby("shop")["promo_starts"].transform("sum")
        table["starts_share"] = (table["promo_starts"] + alpha) / (total_starts + 7 * alpha)
        table["lift"] = table["starts_share"] / (1 / 7)
        table["starts_per_day"] = (
            table["promo_starts"] / table["days_observed"].replace(0, pd.NA)
        )
        table["reliable"] = table["days_observed"] >= 2
        frames.append(table)

    result = pd.concat(frames, ignore_index=True)
    result["weekday_name"] = result["weekday"].map(dict(enumerate(WEEKDAY_NAMES)))
    return (
        result[[
            "shop", "weekday", "weekday_name", "days_observed", "promo_starts",
            "starts_per_day", "active_per_day", "starts_share", "lift", "reliable",
        ]]
        .sort_values(["shop", "weekday"])
        .reset_index(drop=True)
        .round(4)
    )
