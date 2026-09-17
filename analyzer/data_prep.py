"""Слой подготовки данных: загрузка CSV, фичи, сворачивание снимков в состояния.

В дальнейшем load_products будет уметь читать и из ClickHouse
(заменить тело функции, интерфейс сохранится), поэтому весь анализ
зависит только от DataFrame.
"""

import pandas as pd  # pyright: ignore[reportMissingModuleSource]
from typing import cast

from analyzer.config import (
    DEFAULT_MIN_GAP_HOURS,
    GAP_INTERVAL_FACTOR,
    MAX_GAP_HOURS,
    assign_category,
    assign_group,
)

REQUIRED_COLUMNS = {
    "canonical_product_id",
    "name",
    "shop",
    "date",
    "original_price",
    "promotion_price",
    "match_status",
}


def load_products(path: str) -> pd.DataFrame:
    """Читает CSV нормализованных продуктов и проверяет схему."""
    df = pd.read_csv(path, parse_dates=["date"])

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"В файле нет колонок: {sorted(missing)}")

    df = cast(
        pd.DataFrame,
        df[(df["original_price"] > 0) & (df["promotion_price"] > 0)],
    )
    return df.sort_values(by=["date"]).reset_index(drop=True)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """Добавляет производные поля: категория, группа, глубина скидки, день недели."""
    out = df.copy()
    out["group"] = [assign_group(name) for name in out["name"]]
    out["category"] = [
        assign_category(shop, group) for shop, group in zip(out["shop"], out["group"])
    ]
    out["discount_pct"] = (1 - out["promotion_price"] / out["original_price"]) * 100
    out["day"] = out["date"].dt.date
    out["weekday"] = out["date"].dt.dayofweek  # 0 = понедельник
    return out


def median_interval_hours(features: pd.DataFrame) -> float:
    """Типичный интервал опроса парсера, часы.

    Берётся нижний квартиль, а не медиана: в ряду вперемешку лежат
    регулярные опросы (10 минут) и длинные разрывы, когда товар выпадал
    из выдачи. Медиана на коротких рядах уезжает в разрыв и раздувает
    порог склейки эпизодов.
    """
    deltas: pd.Series = (
        features.sort_values("date")
        .groupby(["canonical_product_id", "shop"])["date"]
        .diff()
        .dt.total_seconds()
        .dropna()
    )
    deltas = deltas[deltas > 0]
    if deltas.empty:
        return 0.0
    return float(deltas.quantile(0.25)) / 3600


def resolve_gap_hours(features: pd.DataFrame) -> float:
    """Порог, после которого считаем, что товар выпал из промо-выдачи.

    Из-за SCRAPER_PRODUCT_LIMIT товар регулярно пропадает из листинга
    на 1-2 часа, не прекращая акции (в текущем датасете максимальный
    разрыв — 80 минут при опросе раз в 10 минут). Поэтому порог берём
    заметно больше интервала опроса, иначе один эпизод распадётся
    на десяток фиктивных.
    """
    interval = median_interval_hours(features)
    return min(
        MAX_GAP_HOURS, max(DEFAULT_MIN_GAP_HOURS, interval * GAP_INTERVAL_FACTOR)
    )


def build_states(features: pd.DataFrame, gap_hours: float | None = None) -> pd.DataFrame:
    """Сворачивает повторные снимки в состояния цен, сохраняя хронологию.

    Одно состояние = неизменная пара (original_price, promotion_price)
    на непрерывном отрезке времени. Важно: группировать по значениям цен
    нельзя — если цена ушла и вернулась (A -> B -> A), группировка склеит
    оба отрезка A в одну строку с first_seen из первого и last_seen из
    второго, и событие «цена вернулась» пропадёт. Поэтому состояния
    нарезаются по факту изменения цены в отсортированном по времени ряду.

    Дополнительно ряд режется на эпизоды акций: разрыв в наблюдениях
    больше gap_hours означает, что товар ушёл из промо-выдачи.
    """
    if features.empty:
        return pd.DataFrame(
            columns=[
                "canonical_product_id", "shop", "name", "category", "group",
                "episode_id", "original_price", "promotion_price", "first_seen",
                "last_seen", "n_obs", "match_status", "duration_hours",
                "weight_hours", "discount_pct",
            ]
        )

    gap_hours = resolve_gap_hours(features) if gap_hours is None else gap_hours

    df = features.sort_values(["canonical_product_id", "shop", "date"]).copy()
    keys = ["canonical_product_id", "shop"]

    gap = df.groupby(keys)["date"].diff().dt.total_seconds() / 3600
    first_in_series = df.groupby(keys).cumcount() == 0
    price_changed = (
        (df["original_price"] != df.groupby(keys)["original_price"].shift())
        | (df["promotion_price"] != df.groupby(keys)["promotion_price"].shift())
    ) & ~first_in_series
    episode_break = (gap > gap_hours).fillna(False) | first_in_series

    df["episode_id"] = episode_break.cumsum()
    df["state_id"] = (episode_break | price_changed).cumsum()

    states = (
        df.groupby("state_id", as_index=False)
        .agg(
            canonical_product_id=("canonical_product_id", "first"),
            shop=("shop", "first"),
            name=("name", "first"),
            category=("category", "first"),
            group=("group", "first"),
            episode_id=("episode_id", "first"),
            original_price=("original_price", "first"),
            promotion_price=("promotion_price", "first"),
            first_seen=("date", "min"),
            last_seen=("date", "max"),
            n_obs=("date", "size"),
            match_status=("match_status", lambda s: s.mode().iat[0]),
        )
        .sort_values(["canonical_product_id", "shop", "first_seen"])
        .reset_index(drop=True)
    )

    states["duration_hours"] = (
        states["last_seen"] - states["first_seen"]
    ).dt.total_seconds() / 3600

    # Отрезок из одного наблюдения формально длится 0 часов: при взвешивании
    # по времени он получил бы нулевой вес, поэтому даём ему один интервал опроса.
    min_weight = max(median_interval_hours(features), 1e-6)
    states["weight_hours"] = states["duration_hours"].clip(lower=min_weight)

    states["discount_pct"] = (
        1 - states["promotion_price"] / states["original_price"]
    ) * 100

    return states


def weighted_median(values: pd.Series, weights: pd.Series) -> float:
    """Медиана, взвешенная по времени жизни цены."""
    values = pd.Series(values).astype(float)
    weights = pd.Series(weights).astype(float)
    if values.empty:
        return float("nan")
    ordered = pd.DataFrame({"value": values, "weight": weights}).sort_values(
        "value"
    )
    cumulative = ordered["weight"].cumsum()
    total = cumulative.iloc[-1]
    if total <= 0:
        return float(ordered["value"].median())
    eligible_values = ordered["value"][cumulative >= total / 2]
    return float(eligible_values.iloc[0])


def weighted_mean(values: pd.Series, weights: pd.Series) -> float:
    values = pd.Series(values).astype(float)
    weights = pd.Series(weights).astype(float)
    total = weights.sum()
    if total <= 0:
        return float(values.mean())
    return float((values * weights).sum() / total)


def coverage(features: pd.DataFrame) -> pd.DataFrame:
    """Сколько данных реально собрано по каждому магазину.

    Любая метрика ниже читается только вместе с этой таблицей: на окне
    в несколько дней «вероятность акции по дню недели» ещё не определена,
    и об этом честнее написать в отчёте, чем показать красивое число.
    """
    rows = features.groupby("shop", as_index=False).agg(
        products=("canonical_product_id", "nunique"),
        observations=("date", "size"),
        first_seen=("date", "min"),
        last_seen=("date", "max"),
        days_covered=("day", "nunique"),
        weekdays_covered=("weekday", "nunique"),
    )
    rows["window_hours"] = (
        (rows["last_seen"] - rows["first_seen"]).dt.total_seconds() / 3600
    ).round(2)
    rows["weeks_covered"] = (rows["window_hours"] / 168).round(3)
    rows["weekday_analysis_ready"] = rows["weeks_covered"] >= 2
    return rows.sort_values("observations", ascending=False).reset_index(drop=True)
