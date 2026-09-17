"""Юнит-тесты анализатора на синтетических данных (без ClickHouse)."""

import pandas as pd
from math import isclose

from analyzer.competitors import competitor_comparison, shop_price_index
from analyzer.config import assign_category, assign_group
from analyzer.data_prep import add_features, build_states, coverage
from analyzer.fake_promo import detect_fake_promos
from analyzer.price_history import discount_stats, promo_episodes
from analyzer.weekday_lift import weekday_lift


def make_df(rows):
    df = pd.DataFrame(
        rows,
        columns=[
            "canonical_product_id", "name", "shop", "date",
            "original_price", "promotion_price", "match_status",
        ],
    )
    df["date"] = pd.to_datetime(df["date"])
    return df


def prepared(rows):
    return build_states(add_features(make_df(rows)))


# --- Слой состояний ----------------------------------------------------------

def test_repeated_snapshots_collapse():
    """Парсер пишет ту же цену каждые 10 минут — это одно состояние."""
    rows = [
        ("p1", "Молоко", "lenta", f"2026-09-16 10:{m:02d}", 100, 80, "provisional")
        for m in (0, 10, 20, 30)
    ]
    states = prepared(rows)
    assert len(states) == 1
    assert states.iloc[0]["n_obs"] == 4


def test_returning_price_is_not_merged():
    """A -> B -> A должно дать ТРИ состояния, а не два.

    Группировка по значениям цен склеивала оба отрезка A в одну строку
    и прятала возврат цены — ровно то событие, которое ищет блок лже-акций.
    """
    rows = [
        ("p1", "Молоко", "lenta", "2026-09-16 10:00", 100, 80, "provisional"),
        ("p1", "Молоко", "lenta", "2026-09-16 10:10", 100, 60, "provisional"),
        ("p1", "Молоко", "lenta", "2026-09-16 10:20", 100, 80, "provisional"),
    ]
    states = prepared(rows).sort_values("first_seen")
    assert len(states) == 3
    assert list(states["promotion_price"]) == [80, 60, 80]


def test_long_gap_splits_episodes():
    rows = [
        ("p1", "Молоко", "lenta", "2026-09-16 10:00", 100, 80, "provisional"),
        ("p1", "Молоко", "lenta", "2026-09-16 10:10", 100, 80, "provisional"),
        ("p1", "Молоко", "lenta", "2026-09-18 10:00", 100, 80, "provisional"),
    ]
    assert len(promo_episodes(prepared(rows))) == 2


def test_short_gap_does_not_split_episode():
    """Товар выпал из выдачи на час (SCRAPER_PRODUCT_LIMIT) — акция та же."""
    rows = [
        ("p1", "Молоко", "lenta", "2026-09-16 10:00", 100, 80, "provisional"),
        ("p1", "Молоко", "lenta", "2026-09-16 10:10", 100, 80, "provisional"),
        ("p1", "Молоко", "lenta", "2026-09-16 11:30", 100, 80, "provisional"),
    ]
    assert len(promo_episodes(prepared(rows))) == 1


# --- Категории ---------------------------------------------------------------

def test_category_follows_name_not_shop():
    """mvideo отдаёт мотошины и погремушки — категория должна идти от названия."""
    group = assign_group("Шина для мотоциклов Anlas Capra-R 100/90-19 57H")
    assert group == "авто и мото"
    assert assign_category("mvideo", group) == "прочее"
    assert assign_category("mvideo", assign_group("Телевизор Samsung")) == "электроника"


# --- Блок Б: лже-акции -------------------------------------------------------

def test_old_price_jump_is_detected():
    """«Старая» цена выросла на 20 %, промо-цена та же."""
    rows = [
        ("p1", "Тест", "shopA", "2026-09-16 10:00", 1000, 900, "provisional"),
        ("p1", "Тест", "shopA", "2026-09-16 12:00", 1200, 900, "provisional"),
    ]
    row = detect_fake_promos(prepared(rows)).iloc[0]
    assert "old_price_jump" in row["flags"]
    assert row["level"] in ("suspicious", "likely_fake")
    assert "growth_pct" in row["evidence"]


def test_stable_prices_are_ok():
    rows = [
        ("p1", "Тест", "shopA", "2026-09-16 10:00", 1000, 950, "provisional"),
        ("p1", "Тест", "shopA", "2026-09-16 12:00", 1000, 950, "provisional"),
    ]
    assert detect_fake_promos(prepared(rows)).iloc[0]["level"] == "ok"


def test_deep_but_honest_discount_is_ok():
    """Скидка 70 % без единого изменения цены — не повод обвинять магазин.

    Старое правило discount_spike (>= 60 %) метило такие товары как
    подозрительные; теперь глубина идёт справочным флагом, а не в скор.
    """
    rows = [
        ("p1", "Шампунь", "lenta", "2026-09-16 10:00", 1000, 300, "provisional"),
        ("p1", "Шампунь", "lenta", "2026-09-16 12:00", 1000, 300, "provisional"),
    ]
    row = detect_fake_promos(prepared(rows)).iloc[0]
    assert row["level"] == "ok"
    assert row["suspicion_score"] == 0.0
    assert "deep_discount_for_group" in row["notes"]


def test_both_prices_rise_flag():
    """Обе цены выросли на 15 %, глубина скидки не изменилась — эскалация."""
    rows = [
        ("p1", "Тест", "shopA", "2026-09-16 10:00", 1000, 900, "provisional"),
        ("p1", "Тест", "shopA", "2026-09-16 12:00", 1150, 1035, "provisional"),
    ]
    row = detect_fake_promos(prepared(rows)).iloc[0]
    assert "both_prices_rise" in row["flags"]
    assert "promo_price_rise" in row["flags"]


def test_score_is_normalized():
    rows = [
        ("p1", "Тест", "shopA", "2026-09-16 10:00", 1000, 600, "provisional"),
        ("p1", "Тест", "shopA", "2026-09-16 11:00", 1800, 950, "provisional"),
        ("p1", "Тест", "shopA", "2026-09-16 12:00", 2000, 990, "provisional"),
    ]
    score = detect_fake_promos(prepared(rows)).iloc[0]["suspicion_score"]
    assert 0.0 <= score <= 1.0


# --- Блок А: статистика скидок ----------------------------------------------

def test_discount_stats_not_driven_by_polling_frequency():
    """Товар, опрошенный 10 раз, не должен перевешивать опрошенный дважды."""
    rows = [
        ("p1", "Дешёвая скидка", "shopA", f"2026-09-16 {h:02d}:00", 100, 95, "provisional")
        for h in range(10)
    ] + [
        ("p2", "Глубокая скидка", "shopA", "2026-09-16 10:00", 100, 50, "provisional"),
        ("p2", "Глубокая скидка", "shopA", "2026-09-16 11:00", 100, 50, "provisional"),
    ]
    stats = discount_stats(prepared(rows), by="shop").iloc[0]
    assert stats["products"] == 2
    assert isclose(stats["discount_median"], 27.5, abs_tol=0.1)


# --- Блок В: дни недели ------------------------------------------------------

def test_weekday_lift_marks_unobserved_days():
    """Дни недели, которых нет в данных, помечаются, а не выпадают из таблицы."""
    rows = [
        ("p1", "Тест", "shopA", "2026-09-17 10:00", 100, 90, "provisional"),
        ("p1", "Тест", "shopA", "2026-09-17 12:00", 100, 90, "provisional"),
    ]
    features = add_features(make_df(rows))
    result = weekday_lift(features, promo_episodes(build_states(features)))
    assert not result["reliable"].any()
    assert (result["days_observed"] == 0).sum() > 0
    thursday = result[(result["shop"] == "ALL") & (result["weekday"] == 3)].iloc[0]
    assert thursday["promo_starts"] == 1


def test_weekday_normalized_per_observed_day():
    """Два четверга с одной акцией в каждый — это 1 старт в день, а не 2."""
    rows = []
    for day in ("2026-09-10", "2026-09-17"):
        rows += [
            ("p1", "Тест", "shopA", f"{day} 10:00", 100, 90, "provisional"),
            ("p1", "Тест", "shopA", f"{day} 12:00", 100, 90, "provisional"),
        ]
    features = add_features(make_df(rows))
    result = weekday_lift(features, promo_episodes(build_states(features)))
    thursday = result[(result["shop"] == "ALL") & (result["weekday"] == 3)].iloc[0]
    assert thursday["days_observed"] == 2
    assert isclose(thursday["starts_per_day"], 1.0)
    assert bool(thursday["reliable"])


# --- Блок В: конкуренты ------------------------------------------------------

def test_competitors_empty_without_cross_shop_matches():
    rows = [
        ("p1", "Тест", "shopA", "2026-09-16 10:00", 100, 90, "provisional"),
        ("p2", "Другое", "shopB", "2026-09-16 10:00", 100, 80, "provisional"),
    ]
    states = prepared(rows)
    assert competitor_comparison(states).empty
    assert shop_price_index(states).empty      # индекс 1.0 у всех был бы враньём


def test_competitors_found_when_product_in_two_shops():
    rows = [
        ("p1", "Тест", "shopA", "2026-09-16 10:00", 200, 100, "confirmed"),
        ("p1", "Тест", "shopB", "2026-09-16 10:00", 200, 125, "confirmed"),
    ]
    result = competitor_comparison(prepared(rows)).iloc[0]
    assert result["best_shop"] == "shopA"
    assert isclose(result["dispersion_pct"], 25.0)
    assert bool(result["reliable"])


# --- Служебное ---------------------------------------------------------------

def test_coverage_flags_short_window():
    rows = [
        ("p1", "Тест", "shopA", "2026-09-16 10:00", 100, 90, "provisional"),
        ("p1", "Тест", "shopA", "2026-09-16 12:00", 100, 90, "provisional"),
    ]
    row = coverage(add_features(make_df(rows))).iloc[0]
    assert not bool(row["weekday_analysis_ready"])
