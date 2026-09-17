"""Сводный отчёт: собирает все блоки анализа в один набор таблиц."""

import os

import pandas as pd  # pyright: ignore[reportMissingModuleSource]

from analyzer.competitors import (
    competitor_comparison,
    group_price_comparison,
    shop_price_index,
)
from analyzer.data_prep import add_features, build_states, coverage
from analyzer.fake_promo import FakePromoConfig, detect_fake_promos
from analyzer.price_history import (
    discount_stats,
    price_timeline,
    product_promo_summary,
    promo_episodes,
)
from analyzer.promo_patterns import favorite_products, group_promo_activity
from analyzer.weekday_lift import weekday_lift


def build_report(
    products: pd.DataFrame,
    top_n_favorites: int = 20,
    fake_config: FakePromoConfig | None = None,
) -> dict[str, pd.DataFrame]:
    """Полный прогон анализа по сырым нормализованным данным."""
    features = add_features(products)
    states = build_states(features)
    episodes = promo_episodes(states)

    return {
        # служебное: без этой таблицы остальные числа нельзя интерпретировать
        "coverage": coverage(features),
        # блок А
        "price_timeline": price_timeline(states),
        "product_summary": product_promo_summary(states),
        "promo_episodes": episodes,
        "discount_by_shop": discount_stats(states, by="shop"),
        "discount_by_category": discount_stats(states, by="category"),
        "discount_by_group": discount_stats(states, by="group"),
        # блок Б
        "fake_promos": detect_fake_promos(states, fake_config),
        # блок В
        "promo_favorites": favorite_products(states, top_n=top_n_favorites),
        "group_promo_activity": group_promo_activity(states),
        "weekday_lift": weekday_lift(features, episodes),
        "competitors": competitor_comparison(states),
        "shop_price_index": shop_price_index(states),
        "group_price_comparison": group_price_comparison(states),
    }


def export_report(report: dict[str, pd.DataFrame], out_dir: str) -> list[str]:
    """Сохраняет все таблицы отчёта в CSV, возвращает список путей."""
    os.makedirs(out_dir, exist_ok=True)

    paths = []
    for name, table in report.items():
        path = os.path.join(out_dir, f"{name}.csv")
        table.to_csv(path, index=False, encoding="utf-8-sig")
        paths.append(path)
    return paths
