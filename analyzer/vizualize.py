"""Отрисовка графиков по результатам анализа.

Модуль строит единый набор диаграмм для трёх блоков анализа (история цен,
лже-акции, промо-паттерны) и собирает их в одну самодостаточную HTML-страницу
(картинки встроены как base64 — файл открывается сам по себе, без сервера).
Каждый PNG сохраняется отдельно, чтобы его можно было вставить в презентацию.

Запуск:
    python -m analyzer.visualize --input data/all_normalized_products.csv --out charts

Требует matplotlib (в основной пайплайн проекта не входит — см.
requirements-analytics.txt, чтобы не тащить эту зависимость в докер-образ
скрапера и нормализатора).
"""

import argparse
import base64
import os
import textwrap
from io import BytesIO
from typing import Any, cast

import matplotlib  # pyright: ignore[reportMissingModuleSource]

matplotlib.use("Agg")  # без дисплея: сервер/CI/контейнер
import matplotlib.pyplot as plt  # pyright: ignore[reportMissingModuleSource]
import numpy as np  # pyright: ignore[reportMissingImports]
import pandas as pd  # pyright: ignore[reportMissingModuleSource]
from matplotlib.patches import Patch  # pyright: ignore[reportMissingModuleSource]

from analyzer.data_prep import add_features, build_states, coverage, load_products
from analyzer.price_history import promo_episodes
from analyzer.reports import build_report

# --------------------------------------------------------------------------
# Единый стиль: один раз настраиваем внешний вид, дальше графики просто
# наследуют его. Цель — чтобы весь набор смотрелся как один отчёт, а не
# как случайные графики из разных источников.
# --------------------------------------------------------------------------

SHOP_COLORS = {
    "lenta": "#1f8a70",
    "magnit": "#e8543e",
    "podruzhka": "#d6558c",
    "iledebeaute": "#7c5cbf",
    "mvideo": "#2e6fd9",
    "holodilnik": "#2ba7a1",
    "ALL": "#4a4a4a",
}
LEVEL_COLORS = {"ok": "#8fbf7f", "suspicious": "#f2b134", "likely_fake": "#d64545"}
NEUTRAL = "#4a4a4a"
GRID_COLOR = "#e3e3e3"
FIG_BG = "#ffffff"


def shop_color(shop: str) -> str:
    return SHOP_COLORS.get(shop, "#8c8c8c")


def apply_style() -> None:
    plt.rcParams.update({
        "figure.facecolor": FIG_BG,
        "axes.facecolor": FIG_BG,
        "axes.edgecolor": "#999999",
        "axes.grid": True,
        "axes.grid.axis": "x",
        "axes.axisbelow": True,
        "grid.color": GRID_COLOR,
        "grid.linewidth": 0.8,
        "font.size": 11,
        "font.family": "DejaVu Sans",  # поддерживает кириллицу «из коробки»
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.spines.left": False,
        "xtick.color": "#555555",
        "ytick.color": "#333333",
        "figure.titlesize": 15,
        "figure.titleweight": "bold",
    })


def _finalize(fig, ax_or_axes, title, subtitle=None, note=None):
    """Единое оформление заголовка/подписи/сноски для любого графика."""
    fig_width_inches = fig.get_size_inches()[0]
    fig.suptitle(title, x=0.02, y=0.985, ha="left", va="top",
                 fontsize=14, fontweight="bold", color="#1a1a1a")
    if subtitle:
        wrap_width = max(40, int(fig_width_inches * 11))
        subtitle_wrapped = "\n".join(textwrap.wrap(subtitle, width=wrap_width))
        n_lines = subtitle_wrapped.count("\n") + 1
        fig.text(0.02, 0.925, subtitle_wrapped, ha="left", va="top", fontsize=10, color="#666666")
    else:
        n_lines = 0
    if note:
        wrapped = "\n".join(textwrap.wrap(note, width=max(60, int(fig_width_inches * 13))))
        fig.text(0.02, 0.01, wrapped, ha="left", va="bottom", fontsize=8.5,
                  color="#8a8a8a", style="italic")
    top = (0.80 - 0.035 * max(0, n_lines - 1)) if subtitle else 0.87
    bottom = 0.16 if note else 0.08
    fig.subplots_adjust(top=top, bottom=bottom)


def _save(fig, out_dir, filename):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    fig.savefig(path, dpi=150, facecolor=FIG_BG)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------
# Блок А: история цен и глубина скидки
# --------------------------------------------------------------------------

def chart_coverage(cov: pd.DataFrame, out_dir: str) -> str:
    """Сколько данных реально собрано — читать все следующие графики нужно
    вместе с этим: короткое окно означает широкие интервалы неопределённости."""
    cov = cov.sort_values("observations", ascending=True)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.2))

    colors = [shop_color(s) for s in cov["shop"]]
    ax1.barh(cov["shop"], cov["observations"], color=colors)
    for y, (obs, prod) in enumerate(zip(cov["observations"], cov["products"])):
        ax1.text(obs + max(cov["observations"]) * 0.01, y, f"{obs} набл. / {prod} тов.",
                  va="center", fontsize=9, color="#444444")
    ax1.set_title("Объём собранных данных", fontsize=11, loc="left", color="#333333")
    ax1.set_xlim(0, cov["observations"].max() * 1.35)

    ax2.barh(cov["shop"], cov["window_hours"], color=colors, alpha=0.85)
    for y, hours in enumerate(cov["window_hours"]):
        ax2.text(hours + cov["window_hours"].max() * 0.03, y, f"{hours:.1f} ч",
                  va="center", fontsize=9, color="#444444")
    ax2.set_title("Длительность окна наблюдения", fontsize=11, loc="left", color="#333333")
    ax2.set_xlim(0, cov["window_hours"].max() * 1.4)
    fig.subplots_adjust(wspace=0.45)

    ready = cov["weekday_analysis_ready"].any() if "weekday_analysis_ready" in cov else False
    note = (
        "Окно наблюдения короче 2 недель — оценки по дням недели ниже носят предварительный характер."
        if not ready else
        "Накоплено достаточно недель для устойчивой оценки активности по дням недели."
    )
    _finalize(fig, (ax1, ax2), "Покрытие данных по магазинам",
              "Основа для интерпретации всех остальных графиков", note)
    return _save(fig, out_dir, "00_coverage.png")


def chart_discount_by(states: pd.DataFrame, by: str, out_dir: str,
                       title: str, top_n: int | None = None) -> str | None:
    """Boxplot глубины скидки по магазину/категории/группе — по реальному
    распределению состояний цен, а не только по медиане."""
    groups = states.groupby(by)["discount_pct"]
    order = groups.median().sort_values(ascending=False)
    if top_n:
        order = order.head(top_n)
    labels = order.index.tolist()
    if not labels:
        return None
    data = [groups.get_group(label).values for label in labels]
    sizes = [len(groups.get_group(label)) for label in labels]

    fig_h = max(3.5, 0.45 * len(labels) + 1.4)
    fig, ax = plt.subplots(figsize=(10, fig_h))
    positions = np.arange(len(labels))[::-1]

    bp = cast(Any, ax.boxplot(
        data, positions=positions, vert=False, widths=0.55, patch_artist=True,
        medianprops=dict(color="#1a1a1a", linewidth=1.6),
        whiskerprops=dict(color="#888888"), capprops=dict(color="#888888"),
        flierprops=dict(marker="o", markersize=3, markerfacecolor="#bbbbbb",
                         markeredgecolor="none", alpha=0.6),
    ))
    for patch, label in zip(bp["boxes"], labels):
        color = shop_color(label) if by == "shop" else "#5b8fd9"
        patch.set_facecolor(color)
        patch.set_alpha(0.55)
        patch.set_edgecolor(color)

    ax.set_yticks(positions)
    ytick_labels = [f"{label}  (n={n})" for label, n in zip(labels, sizes)]
    ax.set_yticklabels(ytick_labels)
    ax.set_xlabel("Глубина скидки, %")
    ax.set_xlim(left=0)
    max_len = max(len(t) for t in ytick_labels)
    fig.subplots_adjust(left=min(0.42, 0.09 + max_len * 0.0075))

    _finalize(fig, ax, title,
              "Медиана, межквартильный размах и выбросы по состояниям цен",
              "n — число товарных позиций (состояний цен), по которым построено распределение.")
    fname = f"01_discount_by_{by}.png"
    return _save(fig, out_dir, fname)


def chart_price_timelines(states: pd.DataFrame, out_dir: str, max_products: int = 6) -> str | None:
    """История цены по конкретным товарам: ступенчатый график original/promo.

    В приоритете — товары, у которых цена реально менялась (это и есть
    материал для блока «лже-акции»); если таких нет, показываем самые
    активные по числу эпизодов, честно отмечая, что цена была стабильна.
    """
    per_product = states.groupby(["canonical_product_id", "shop"])
    changed = [key for key, g in per_product if len(g) > 1]
    if changed:
        chosen = changed[:max_products]
        stable_note = ""
    else:
        top = (
            states.groupby(["canonical_product_id", "shop"])["n_obs"].sum()
            .sort_values(ascending=False).head(max_products).index.tolist()
        )
        chosen = top
        stable_note = " Цена ни у одного товара пока не менялась — линии постоянны по определению."

    if not chosen:
        return None

    n = len(chosen)
    ncols = 2 if n > 1 else 1
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(6.5 * ncols, max(3.6, 3.1 * nrows)), squeeze=False)
    axes_flat = axes.flatten()

    for ax, (pid, shop) in zip(axes_flat, chosen):
        g = per_product.get_group((pid, shop)).sort_values("first_seen")
        name = g["name"].iloc[0]
        color = shop_color(shop)
        prev_end = None
        for _, row in g.iterrows():
            x = [row["first_seen"], row["last_seen"]]
            if prev_end is not None and (row["first_seen"] - prev_end).total_seconds() > 0:
                ax.axvline(row["first_seen"], color="#cccccc", linestyle=":", linewidth=1)
            ax.plot(x, [row["promotion_price"]] * 2, color=color, linewidth=2.6, solid_capstyle="round")
            ax.plot(x, [row["original_price"]] * 2, color=color, linewidth=1.2,
                     linestyle="--", alpha=0.6)
            ax.fill_between(x, row["promotion_price"], row["original_price"],
                             color=color, alpha=0.08)
            prev_end = row["last_seen"]
        ax.set_title(textwrap.shorten(f"{name} · {shop}", width=48), fontsize=9.5, loc="left")
        ax.tick_params(axis="x", labelsize=7.5, rotation=15)
        ax.set_ylabel("₽", fontsize=9, rotation=0, labelpad=12, va="center")

    for ax in axes_flat[len(chosen):]:
        ax.axis("off")

    note = ("Разрыв в линии = товар пропадал из промо-выдачи." + stable_note).strip()
    _finalize(fig, axes_flat, "История цены: примеры товаров",
              "Сплошная — цена к оплате, пунктир — «старая» цена, заливка — размер скидки",
              note)
    fig.subplots_adjust(hspace=0.55, wspace=0.28)
    return _save(fig, out_dir, "02_price_timelines.png")


# --------------------------------------------------------------------------
# Блок Б: лже-акции
# --------------------------------------------------------------------------

def chart_fake_promo_levels(fake_promos: pd.DataFrame, out_dir: str) -> str | None:
    """Сколько товаров попало в каждый уровень подозрительности — всего
    и в разбивке по магазинам, плюс частота отдельных сигналов."""
    if fake_promos.empty:
        return None
    order = ["ok", "suspicious", "likely_fake"]
    labels_ru = {"ok": "чисто", "suspicious": "подозрительно", "likely_fake": "вероятная лже-акция"}

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.6))

    pivot = (
        fake_promos.groupby(["shop", "level"]).size().unstack(fill_value=0)
        .reindex(columns=order, fill_value=0)
    )
    pivot = pivot.loc[pivot.sum(axis="columns").sort_values(ascending=True).index]
    bottom = np.zeros(len(pivot))
    for level in order:
        values = pivot[level].values
        ax1.barh(pivot.index, values, left=bottom, color=LEVEL_COLORS[level],
                  label=labels_ru[level])
        bottom += values
    ax1.set_title("Уровень подозрительности по магазинам", fontsize=11, loc="left", color="#333333")
    ax1.set_xlim(0, pivot.T.sum(axis=0).max() * 1.08)
    ax1.legend(loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=3, fontsize=8.5, frameon=False)

    flags = cast(
        pd.Series,
        fake_promos["flags"].fillna("").str.split("|").explode(),
    )
    flags = flags[flags != ""].value_counts()
    if not flags.empty:
        ax2.barh(flags.index[::-1], flags.values[::-1], color="#d6558c")
        for y, v in enumerate(flags.astype(int).tolist()[::-1]):
            ax2.text(v + 0.05, y, str(int(v)), va="center", fontsize=9)
        ax2.set_title("Частота сработавших признаков", fontsize=11, loc="left", color="#333333")
    else:
        ax2.text(0.5, 0.5, "Ни один структурный признак\nне сработал за период наблюдения",
                  ha="center", va="center", fontsize=11, color="#888888", transform=ax2.transAxes)
        ax2.axis("off")

    total = len(fake_promos)
    n_flag = (fake_promos["level"] != "ok").sum()
    note = (
        f"Признаки ищутся ВНУТРИ активной акции (пропуска цены нет — базовой цены товара без "
        f"скидки парсер не собирает). Всего проверено {total} товаров, помечено {n_flag}."
    )
    _finalize(fig, (ax1, ax2), "Блок Б: детекция лже-акций", None, note)
    fig.subplots_adjust(bottom=0.22)
    return _save(fig, out_dir, "10_fake_promo_levels.png")


def chart_fake_promo_scatter(fake_promos: pd.DataFrame, out_dir: str) -> str | None:
    """Скор подозрительности vs заявленная глубина скидки: где искать риски."""
    if fake_promos.empty:
        return None
    fig, ax = plt.subplots(figsize=(9, 5.5))
    for shop, g in fake_promos.groupby("shop"):
        shop_name = str(shop)
        ax.scatter(g["discount_max"], g["suspicion_score"], s=55, alpha=0.75,
                   color=shop_color(shop_name), label=shop_name, edgecolor="white", linewidth=0.4)
    ax.axhline(0.30, color="#f2b134", linestyle="--", linewidth=1, alpha=0.8)
    ax.axhline(0.60, color="#d64545", linestyle="--", linewidth=1, alpha=0.8)
    ax.text(ax.get_xlim()[1] * 0.99, 0.30, " suspicious", va="bottom", ha="right",
            fontsize=8, color="#a97c00")
    ax.text(ax.get_xlim()[1] * 0.99, 0.60, " likely_fake", va="bottom", ha="right",
            fontsize=8, color="#a02020")
    ax.set_xlabel("Максимальная заявленная скидка, %")
    ax.set_ylabel("Скор подозрительности")
    ax.set_ylim(-0.03, 1.05)
    ax.legend(loc="upper left", fontsize=8.5, frameon=False, ncol=2)
    _finalize(fig, ax, "Скор подозрительности и глубина скидки",
              "Каждая точка — товар в конкретном магазине",
              "Глубокая скидка сама по себе не подозрительна: об этом говорит положение по X, "
              "подозрительность — положение по Y.")
    return _save(fig, out_dir, "11_fake_promo_scatter.png")


# --------------------------------------------------------------------------
# Блок В: любимые промо, дни недели, конкуренты
# --------------------------------------------------------------------------

def chart_favorites(favorites: pd.DataFrame, out_dir: str, top_n: int = 15) -> str | None:
    if favorites.empty:
        return None
    top = favorites.head(top_n).sort_values("score", ascending=True)
    fig, ax = plt.subplots(figsize=(10, max(3.5, 0.4 * len(top) + 1.2)))
    colors = [shop_color(s) for s in top["shop"]]
    bars = ax.barh(range(len(top)), top["score"], color=colors)
    ax.set_yticks(range(len(top)))
    ytick_labels = [
        textwrap.shorten(f"{n} ({s})", width=42) for n, s in zip(top["name"], top["shop"])
    ]
    ax.set_yticklabels(ytick_labels, fontsize=9)
    for bar, discount in zip(bars, top["median_discount"]):
        ax.text(bar.get_width() + 0.01, bar.get_y() + bar.get_height() / 2,
                f"скидка {discount:.0f}%", va="center", fontsize=8.5, color="#555555")
    ax.set_xlim(0, top["score"].max() * 1.35)
    ax.set_xlabel("Индекс промо-активности (0–1)")
    max_len = max(len(t) for t in ytick_labels)
    fig.subplots_adjust(left=min(0.5, 0.06 + max_len * 0.0095))

    legend_handles = [Patch(facecolor=c, label=s) for s, c in SHOP_COLORS.items() if s != "ALL"]
    fig.legend(handles=legend_handles, loc="upper center", bbox_to_anchor=(0.5, 0.09),
               ncol=6, fontsize=8, frameon=False)

    _finalize(fig, ax, "Топ «любимых» промо-товаров",
              "Комбинация длительности акции, числа пересмотров цены и глубины скидки",
              "Ранг считается внутри магазина: сравнивать индекс между разными магазинами напрямую нельзя.")
    fig.subplots_adjust(bottom=0.22)
    return _save(fig, out_dir, "20_favorites.png")


def chart_weekday(weekday_df: pd.DataFrame, out_dir: str, shop: str = "ALL") -> str | None:
    """Активность акций по дням недели: столбцы + штриховка ненаблюдённых дней."""
    df = cast(
        pd.DataFrame,
        weekday_df[weekday_df["shop"] == shop],
    ).sort_values(by=["weekday"])
    if df.empty:
        return None
    fig, ax = plt.subplots(figsize=(9, 4.5))
    colors = ["#5b8fd9" if reliable else "#cfd8e3" for reliable in df["reliable"]]
    bars = ax.bar(df["weekday_name"], df["lift"], color=colors, edgecolor="#ffffff", linewidth=0.6)
    for bar, reliable, days in zip(bars, df["reliable"], df["days_observed"]):
        if not reliable:
            bar.set_hatch("//")
            bar.set_edgecolor("#9aa7b4")
    ax.axhline(1.0, color="#888888", linestyle="--", linewidth=1)
    ax.text(-0.45, 1.15, "средний уровень", fontsize=8, color="#888888", ha="left")
    ax.set_ylabel("Lift (1.0 = равномерно по неделе)")

    legend_handles = [
        Patch(facecolor="#5b8fd9", label="день отнаблюдён ≥ 2 раз"),
        Patch(facecolor="#cfd8e3", hatch="//", edgecolor="#9aa7b4", label="данных недостаточно"),
    ]
    ax.legend(handles=legend_handles, loc="upper right", fontsize=8.5, frameon=False)

    reliable_days = int(df["reliable"].sum())
    note = (
        f"Заштрихованные столбцы посчитаны по {int(df[~df['reliable']]['days_observed'].max() or 0)} "
        f"наблюдению и ниже — доверять им нельзя. Надёжных дней недели: {reliable_days} из 7."
        if reliable_days < 7 else
        "Оценка построена на достаточном числе недель по каждому дню."
    )
    _finalize(fig, ax, f"Промо-активность по дням недели — {shop}",
              "Метрика — старты акций (устраняет автокорреляцию многодневных акций)", note)
    return _save(fig, out_dir, f"21_weekday_{shop}.png")


def chart_group_shop_heatmap(group_price_comparison: pd.DataFrame, out_dir: str,
                              top_n_groups: int = 14) -> str | None:
    """Тепловая карта медианной скидки: группа товаров × магазин.

    Работает без кросс-магазинного матчинга: сравнивает магазины по тому,
    насколько глубоко они скидывают в одной и той же товарной категории.
    """
    if group_price_comparison.empty:
        return None
    top_groups = (
        group_price_comparison.groupby("group")["products"].sum()
        .sort_values(ascending=False).head(top_n_groups).index
    )
    subset = group_price_comparison[group_price_comparison["group"].isin(top_groups)]
    pivot = subset.pivot_table(index="group", columns="shop", values="median_discount")
    pivot = pivot.loc[pivot.mean(axis=1).sort_values(ascending=False).index]

    fig, ax = plt.subplots(figsize=(1.3 * len(pivot.columns) + 3, 0.5 * len(pivot) + 2))
    masked = np.ma.masked_invalid(pivot.values)
    im = ax.imshow(masked, cmap="YlOrRd", aspect="auto", vmin=0, vmax=max(70, np.nanmax(pivot.values)))

    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns, rotation=30, ha="right")
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index, fontsize=9)
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    max_len = max(len(str(g)) for g in pivot.index)
    fig.subplots_adjust(left=min(0.4, 0.08 + max_len * 0.009))

    for i in range(pivot.shape[0]):
        for j in range(pivot.shape[1]):
            value = pivot.values[i, j]
            if np.isnan(value):
                ax.text(j, i, "—", ha="center", va="center", fontsize=8, color="#bbbbbb")
            else:
                color = "#ffffff" if value > 45 else "#333333"
                ax.text(j, i, f"{value:.0f}%", ha="center", va="center", fontsize=8.5, color=color)

    cbar = fig.colorbar(im, ax=ax, shrink=0.75, pad=0.02)
    cbar.set_label("медианная скидка, %", fontsize=9)

    _finalize(fig, ax, "Кто скидывает глубже: группа товаров × магазин",
              "Заменяет прямое сравнение конкурентов, пока нет подтверждённых кросс-матчей",
              "«—» значит, что магазин не представлен в этой товарной группе.")
    return _save(fig, out_dir, "22_group_shop_heatmap.png")


def chart_competitor_dispersion(competitors: pd.DataFrame, out_dir: str, top_n: int = 20) -> str | None:
    """Прямое сравнение конкурентов по одинаковым товарам (нужен матчинг)."""
    if competitors.empty:
        return None
    top = competitors.sort_values("dispersion_pct", ascending=False).head(top_n)
    top = top.sort_values("dispersion_pct", ascending=True)
    fig, ax = plt.subplots(figsize=(10, max(3.5, 0.4 * len(top) + 1.2)))
    colors = ["#5b8fd9" if reliable else "#cfd8e3" for reliable in top["reliable"]]
    ax.barh(range(len(top)), top["dispersion_pct"], color=colors)
    ax.set_yticks(range(len(top)))
    ax.set_yticklabels([textwrap.shorten(n, width=45) for n in top["name"]], fontsize=9)
    ax.set_xlabel("Разброс цены между магазинами, %")
    fig.subplots_adjust(left=0.36)
    _finalize(fig, ax, "Разброс цен на одинаковые товары между магазинами",
              "Товары с подтверждённым кросс-магазинным матчем", None)
    return _save(fig, out_dir, "23_competitor_dispersion.png")


# --------------------------------------------------------------------------
# Сборка HTML-дашборда
# --------------------------------------------------------------------------

def _img_to_base64(path: str) -> str:
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")


def build_dashboard_html(sections: list, out_path: str, meta: dict) -> str:
    """sections: [(заголовок_блока, [(png_path, caption), ...]), ...]"""
    parts = [f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Промо-аналитика: дашборд</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
          background: #f6f7f9; color: #1f2430; margin: 0; }}
  header {{ background: #1f2430; color: #fff; padding: 28px 40px; }}
  header h1 {{ margin: 0 0 6px 0; font-size: 22px; }}
  header p {{ margin: 0; color: #b7bdcc; font-size: 13px; }}
  .meta {{ display: flex; gap: 24px; margin-top: 14px; flex-wrap: wrap; }}
  .meta div {{ background: #2b3142; padding: 8px 14px; border-radius: 8px; font-size: 12.5px; }}
  .meta b {{ color: #8fd3ff; }}
  main {{ max-width: 1180px; margin: 0 auto; padding: 24px 40px 60px; }}
  section {{ margin-bottom: 42px; }}
  section h2 {{ font-size: 17px; border-left: 4px solid #5b8fd9; padding-left: 10px;
                margin-bottom: 16px; }}
  .card {{ background: #fff; border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,0.08);
           padding: 16px; margin-bottom: 20px; }}
  .card img {{ width: 100%; height: auto; border-radius: 6px; display: block; }}
  .caption {{ font-size: 12.5px; color: #6a7183; margin-top: 10px; }}
  footer {{ text-align: center; color: #9aa1b0; font-size: 12px; padding: 20px; }}
</style>
</head>
<body>
<header>
  <h1>Промо-аналитика по данным парсера</h1>
  <p>История цен · выявление лже-акций · промо-паттерны и сравнение магазинов</p>
  <div class="meta">
    <div>Наблюдений: <b>{meta.get('observations', '—')}</b></div>
    <div>Товаров: <b>{meta.get('products', '—')}</b></div>
    <div>Магазинов: <b>{meta.get('shops', '—')}</b></div>
    <div>Окно: <b>{meta.get('window', '—')}</b></div>
  </div>
</header>
<main>
"""]
    for title, items in sections:
        parts.append(f'<section><h2>{title}</h2>')
        for png_path, caption in items:
            if png_path is None:
                continue
            b64 = _img_to_base64(png_path)
            parts.append(
                f'<div class="card"><img src="data:image/png;base64,{b64}" alt="{caption}">'
                f'<div class="caption">{caption}</div></div>'
            )
        parts.append('</section>')
    parts.append(
        '<footer>Сгенерировано analyzer.visualize · цифры требуют пополнения датасета '
        'для устойчивых выводов по дням недели и конкурентам</footer></main></body></html>'
    )

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(parts))
    return out_path


def build_all(input_path: str, out_dir: str, favorites_top_n: int = 20) -> str:
    apply_style()
    products = load_products(input_path)
    features = add_features(products)
    states = build_states(features)
    episodes = promo_episodes(states)
    report = build_report(products, top_n_favorites=favorites_top_n)

    charts_dir = os.path.join(out_dir, "charts")
    sections = []

    sections.append(("Покрытие данных", [
        (chart_coverage(report["coverage"], charts_dir), "Объём и длительность наблюдения по магазинам"),
    ]))

    sections.append(("Блок А. История цен и глубина скидки", [
        (chart_discount_by(states, "shop", charts_dir, "Глубина скидки по магазинам"),
         "Распределение глубины скидки по магазинам"),
        (chart_discount_by(states, "category", charts_dir, "Глубина скидки по категориям"),
         "Продукты / косметика / электроника / прочее"),
        (chart_discount_by(states, "group", charts_dir, "Глубина скидки по товарным группам", top_n=15),
         "Топ-15 товарных групп по медианной скидке"),
        (chart_price_timelines(states, charts_dir), "Примеры истории цены конкретных товаров"),
    ]))

    sections.append(("Блок Б. Выявление лже-акций", [
        (chart_fake_promo_levels(report["fake_promos"], charts_dir),
         "Распределение уровней подозрительности и частота признаков"),
        (chart_fake_promo_scatter(report["fake_promos"], charts_dir),
         "Скор подозрительности относительно заявленной глубины скидки"),
    ]))

    sections.append(("Блок В. Промо-паттерны и сравнение магазинов", [
        (chart_favorites(report["promo_favorites"], charts_dir), "Топ промо-активных товаров"),
        (chart_weekday(report["weekday_lift"], charts_dir, "ALL"),
         "Активность акций по дням недели, суммарно по всем магазинам"),
        (chart_group_shop_heatmap(report["group_price_comparison"], charts_dir),
         "Сравнение магазинов по глубине скидки внутри товарных групп"),
        (chart_competitor_dispersion(report["competitors"], charts_dir),
         "Прямое сравнение цен на одинаковые товары (нужен подтверждённый матчинг)"),
    ]))

    meta = {
        "observations": len(products),
        "products": products["canonical_product_id"].nunique(),
        "shops": products["shop"].nunique(),
        "window": f"{(features['date'].max() - features['date'].min()).total_seconds() / 3600:.1f} ч",
    }
    dashboard_path = build_dashboard_html(sections, os.path.join(out_dir, "dashboard.html"), meta)
    return dashboard_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Отрисовка графиков по результатам анализа")
    parser.add_argument("--input", required=True, help="CSV из нормализатора")
    parser.add_argument("--out", default="charts", help="папка для PNG и dashboard.html")
    parser.add_argument("--top-favorites", type=int, default=20)
    args = parser.parse_args(argv)

    path = build_all(args.input, args.out, args.top_favorites)
    print(f"Дашборд сохранён: {os.path.abspath(path)}")
    print(f"PNG-графики: {os.path.abspath(os.path.join(args.out, 'charts'))}")


if __name__ == "__main__":
    main()