"""Аналитический модуль проекта (история цен, лже-акции, промо-паттерны)."""

from analyzer.competitors import (
    competitor_comparison,
    group_price_comparison,
    shop_price_index,
)
from analyzer.data_prep import add_features, build_states, coverage, load_products
from analyzer.fake_promo import FakePromoConfig, detect_fake_promos
from analyzer.price_history import (
    discount_stats,
    price_history,
    price_timeline,
    product_promo_summary,
    promo_episodes,
)
from analyzer.promo_patterns import favorite_products, group_promo_activity
from analyzer.reports import build_report, export_report
from analyzer.weekday_lift import weekday_lift

__all__ = [
    "load_products", "add_features", "build_states", "coverage",
    "price_history", "price_timeline", "discount_stats", "promo_episodes",
    "product_promo_summary", "detect_fake_promos", "FakePromoConfig",
    "favorite_products", "group_promo_activity", "weekday_lift",
    "competitor_comparison", "shop_price_index", "group_price_comparison",
    "build_report", "export_report",
]
