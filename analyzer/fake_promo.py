"""Блок Б: детекция «лже-акций».

Работает на состояниях цен (build_states). Датасет содержит только
акционные товары, поэтому базовой (неакционной) цены у нас нет, и
классическое «подняли цену перед скидкой» напрямую не проверяется.
Зато проверяется поведение цен ВНУТРИ активной акции:

1. old_price_jump    — «старая» цена подскочила, промо-цена не изменилась
                       (классическое надувание скидки);
2. promo_price_rise  — цена со скидкой выросла прямо во время акции;
3. both_prices_rise  — обе цены выросли синхронно, глубина скидки та же
                       («эскалация»: покупатель платит больше под тем же бейджем);
4. depth_swing       — глубина скидки скачет внутри одной акции;
5. worse_than_best   — сейчас дороже, чем было в этой же акции, а заявленная
                       скидка не меньше.

Скор нормирован на сумму весов, уровни: ok / suspicious / likely_fake.

Почему выброшены два правила предыдущей версии:

* reference_inflation (original_price > медиана promotion_price × 1.3) —
  в датасете, где ВСЕ строки акционные, медиана промо-цены почти равна самой
  промо-цене, поэтому правило срабатывает автоматически для любой скидки
  глубже ~23 %. На контрольном прогоне оно дало 58 срабатываний из 94,
  то есть измеряло глубину скидки, а не манипуляцию.
* discount_spike (скидка ≥ 60 %) — наказывает за честную глубокую скидку.
  Для косметики 60-70 % — норма, для крупной техники аномалия, поэтому
  «выброс» считается относительно своей товарной группы и выносится
  в справочные флаги, не влияя на скор.
"""

import json

import pandas as pd

from analyzer.config import FakePromoConfig


def _is_psychological(price: float) -> bool:
    """Цена вида 999, 249.99, 1990 — маркетинговый паттерн (справочно)."""
    rubles = int(price)
    kopecks = round(price * 100) % 100
    return kopecks in (99, 90) or rubles % 100 == 99 or rubles % 10 == 9


def _outlier_thresholds(states: pd.DataFrame, quantile: float) -> dict:
    """Порог «аномально глубокой скидки» отдельно для каждой товарной группы."""
    if states.empty:
        return {}
    return (
        states.groupby("group")["discount_pct"]
        .quantile(quantile)
        .to_dict()
    )


def detect_fake_promos(
    states: pd.DataFrame, config: FakePromoConfig | None = None
) -> pd.DataFrame:
    """Возвращает таблицу товаров со скорингом подозрительности."""
    cfg = config or FakePromoConfig()
    if states.empty:
        return pd.DataFrame()

    thresholds = _outlier_thresholds(states, cfg.outlier_quantile)
    rows = []

    for pid, shop in states[["canonical_product_id", "shop"]].drop_duplicates().itertuples(
        index=False, name=None
    ):
        raw = states[
            (states["canonical_product_id"] == pid)
            & (states["shop"] == shop)
        ]
        g = raw.sort_values("first_seen")
        score, flags, notes, evidence = 0.0, [], [], {}

        prev_old = g["original_price"].shift()
        prev_promo = g["promotion_price"].shift()
        prev_discount = (1 - prev_promo / prev_old) * 100

        old_growth = (g["original_price"] / prev_old - 1) * 100
        promo_growth = (g["promotion_price"] / prev_promo - 1) * 100
        depth_shift = (g["discount_pct"] - prev_discount).abs()

        # 1. Скачок «старой» цены при неизменной промо-цене
        jump = (old_growth >= cfg.old_price_jump_pct) & (promo_growth.abs() <= 1.0)
        if jump.any():
            score += cfg.w_old_jump
            flags.append("old_price_jump")
            idx = old_growth[jump].idxmax()
            evidence["old_price_jump"] = {
                "at": str(g.loc[idx, "first_seen"]),
                "old_before": float(prev_old[idx]),
                "old_after": float(g.loc[idx, "original_price"]),
                "growth_pct": round(float(old_growth[idx]), 2),
                "promo_price": float(g.loc[idx, "promotion_price"]),
                "discount_before": round(float(prev_discount[idx]), 2),
                "discount_after": round(float(g.loc[idx, "discount_pct"]), 2),
            }

        # 2. Цена со скидкой выросла во время акции
        promo_up = promo_growth >= cfg.promo_rise_pct
        if promo_up.any():
            score += cfg.w_promo_rise
            flags.append("promo_price_rise")
            idx = promo_growth[promo_up].idxmax()
            evidence["promo_price_rise"] = {
                "at": str(g.loc[idx, "first_seen"]),
                "promo_before": float(prev_promo[idx]),
                "promo_after": float(g.loc[idx, "promotion_price"]),
                "growth_pct": round(float(promo_growth[idx]), 2),
            }

        # 3. Синхронный рост обеих цен при сохранении глубины скидки
        both_rise = (
            (old_growth >= cfg.both_rise_pct)
            & (promo_growth >= cfg.both_rise_pct)
            & (depth_shift <= cfg.both_rise_discount_tol)
        )
        if both_rise.any():
            score += cfg.w_both_rise
            flags.append("both_prices_rise")
            idx = promo_growth[both_rise].idxmax()
            evidence["both_prices_rise"] = {
                "at": str(g.loc[idx, "first_seen"]),
                "old_growth_pct": round(float(old_growth[idx]), 2),
                "promo_growth_pct": round(float(promo_growth[idx]), 2),
                "discount_before": round(float(prev_discount[idx]), 2),
                "discount_after": round(float(g.loc[idx, "discount_pct"]), 2),
                "extra_payment": round(
                    float(g.loc[idx, "promotion_price"] - prev_promo[idx]), 2
                ),
            }

        # 4. Размах глубины скидки внутри акции
        swing = float(g["discount_pct"].max() - g["discount_pct"].min())
        if swing >= cfg.depth_swing_pp:
            score += cfg.w_depth_swing
            flags.append("depth_swing")
            evidence["depth_swing"] = {
                "discount_min": round(float(g["discount_pct"].min()), 2),
                "discount_max": round(float(g["discount_pct"].max()), 2),
                "swing_pp": round(swing, 2),
                "n_states": int(len(g)),
            }

        # 5. Сейчас дороже, чем было в этой же акции, при не меньшей скидке
        best_idx = g["promotion_price"].idxmin()
        last = g.iloc[-1]
        best_price = float(g.loc[best_idx, "promotion_price"])
        overpay = (float(last["promotion_price"]) / best_price - 1) * 100
        if (
            overpay >= cfg.worse_than_best_pct
            and float(last["discount_pct"]) >= float(g.loc[best_idx, "discount_pct"])
        ):
            score += cfg.w_worse_than_best
            flags.append("worse_than_best")
            evidence["worse_than_best"] = {
                "best_price": best_price,
                "best_seen_at": str(g.loc[best_idx, "first_seen"]),
                "best_discount": round(float(g.loc[best_idx, "discount_pct"]), 2),
                "last_price": float(last["promotion_price"]),
                "last_discount": round(float(last["discount_pct"]), 2),
                "overpay_pct": round(overpay, 2),
            }

        # --- справочные признаки: в скор не входят ---
        group_threshold = thresholds.get(g["group"].iloc[0])
        if group_threshold is not None and g["discount_pct"].max() >= group_threshold:
            notes.append("deep_discount_for_group")
        if g["promotion_price"].map(_is_psychological).any():
            notes.append("psych_price")
        if len(g) == 1:
            notes.append("single_state")  # цена не менялась: судить не о чем

        normalized = round(score / cfg.total_weight, 3)
        if normalized >= cfg.likely_fake:
            level = "likely_fake"
        elif normalized >= cfg.suspicious:
            level = "suspicious"
        else:
            level = "ok"

        rows.append({
            "canonical_product_id": pid,
            "shop": shop,
            "name": g["name"].iloc[0],
            "category": g["category"].iloc[0],
            "group": g["group"].iloc[0],
            "n_price_states": int(len(g)),
            "n_episodes": int(g["episode_id"].nunique()),
            "price_first": float(g["promotion_price"].iloc[0]),
            "price_last": float(last["promotion_price"]),
            "discount_now": round(float(last["discount_pct"]), 2),
            "discount_max": round(float(g["discount_pct"].max()), 2),
            "suspicion_score": normalized,
            "level": level,
            "flags": "|".join(flags),
            "notes": "|".join(notes),
            "evidence": json.dumps(evidence, ensure_ascii=False),
        })

    result = pd.DataFrame(rows)
    order = {"likely_fake": 0, "suspicious": 1, "ok": 2}
    return (
        result.assign(_o=result["level"].map(order))
        .sort_values(["_o", "suspicion_score"], ascending=[True, False])
        .drop(columns="_o")
        .reset_index(drop=True)
    )
