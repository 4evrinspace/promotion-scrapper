"""Юнит-тесты анализатора на синтетических данных (без ClickHouse)."""

import pandas as pd
import pytest

from analyzer.data_prep import add_features, build_states
from analyzer.fake_promo import FakePromoConfig, detect_fake_promos
from analyzer.weekday_lift import weekday_lift


def make_df(rows):
    df = pd.DataFrame(
        rows,
        columns=[
            "canonical_product_id",
            "name",
            "shop",
            "date",
            "original_price",
            "promotion_price",
            "match_status",
        ],
    )
    df["date"] = pd.to_datetime(df["date"])
    return df


# --- Блок Б: лже-акции -------------------------------------------------------

def test_old_price_jump_is_detected():
    """«Старая» цена выросла на 20%, промо-цена та же -> likely_fake."""
    df = make_df([
        ("p1", "Тест", "shopA", "2026-09-16 10:00", 1000, 900, "provisional"),
        ("p1", "Тест", "shopA", "2026-09-16 12:00", 1200, 900, "provisional"),
    ])
    result = detect_fake_promos(build_states(add_features(df)))
    row = result.iloc[0]
    assert "old_price_jump" in row["flags"]
    assert row["suspicion_score"] >= FakePromoConfig().likely_fake


def test_stable_prices_are_ok():
    df = make_df([
        ("p1", "Тест", "shopA", "2026-09-16 10:00", 1000, 950, "provisional"),
        ("p1", "Тест", "shopA", "2026-09-16 12:00", 1000, 950, "provisional"),
    ])
    result = detect_fake_promos(build_states(add_features(df)))
    assert result.iloc[0]["level"] == "ok"


def test_both_prices_rise_flag():
    """Обе цены выросли на 15%, глубина скидки не изменилась -> эскалация."""
    df = make_df([
        ("p1", "Тест", "shopA", "2026-09-16 10:00", 1000, 900, "provisional"),
        ("p1", "Тест", "shopA", "2026-09-16 12:00", 1150, 1035, "provisional"),
    ])
    result = detect_fake_promos(build_states(add_features(df)))
    assert "both_prices_rise" in result.iloc[0]["flags"]


# --- Блок В: дни недели ------------------------------------------------------

def test_weekday_lift_smoothing():
    """При данных только за четверг lift не должен улетать бесконечно."""
    df = make_df([
        ("p1", "Тест", "shopA", f"2026-09-{day} 10:00", 100, 90, "provisional")
        for day in (17,)  # одна дата = один день недели
    ])
    result = weekday_lift(add_features(df), alpha=1.0)
    assert result["lift"].max() < 7  # без сглаживания был бы ровно 7
    assert (result["activity_share"].sum() - 1.0) < 1e-6  # доли сходятся к 1