"""Bounded result profiling and interactive chart selection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from html import escape
from numbers import Number
import re
from typing import Any

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_complex_dtype, is_datetime64_any_dtype, is_numeric_dtype


_COLORS = ("#50de8b", "#68aeff", "#a78bfa", "#f4ba72", "#45d0cd", "#ee88b4")
_KINDS = {"bar", "line", "pie", "scatter", "histogram"}
_DATE_VALUE = re.compile(r"^\d{4}-\d{2}(?:-\d{2}(?:[T ].*)?)?$")
_SENSITIVE = re.compile(
    r"(?:^|_)(?:password|passphrase|passwd|secret|token|api_key|email|e_mail|phone|mobile|ssn|"
    r"social_security|street_address|postal_address|full_name|first_name|last_name|customer_name|"
    r"card_number|bank_account|birth_date|date_of_birth|ip_address)(?:_|$)"
)
_SENSITIVE_VALUE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+|(?:\d[ -]?){13,19}|\d{3}-\d{2}-\d{4}")


@dataclass(frozen=True)
class ChartSpec:
    kind: str
    x: str
    y: str | None = None
    color: str | None = None
    title: str | None = None
    currency: str | None = None


def _columns(frame: pd.DataFrame) -> dict[str, pd.Series]:
    labels = pd.Index(frame.columns.map(str))
    unique = ~labels.duplicated(keep=False)
    return {label: frame.iloc[:, index] for index, label in enumerate(labels) if unique[index]}


def _normalized(name: str) -> str:
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return re.sub(r"\W+", "_", expanded.lower()).strip("_")


def _identifier(name: str) -> bool:
    normalized = _normalized(name)
    return normalized == "id" or normalized.endswith(("_id", "_uuid"))


def _numbers(series: pd.Series) -> pd.Series | None:
    if is_bool_dtype(series.dtype) or is_complex_dtype(series.dtype):
        return None
    values = series.dropna()
    if values.empty:
        return None
    if not is_numeric_dtype(series.dtype):
        if not values.map(lambda value: isinstance(value, (Number, Decimal)) and not isinstance(value, (bool, complex))).all():
            return None
    numeric = pd.to_numeric(series, errors="coerce").astype(float)
    finite = numeric.where(np.isfinite(numeric))
    return finite if finite.notna().any() else None


def _dates(series: pd.Series) -> pd.Series | None:
    values = series.dropna()
    if values.empty:
        return None
    if not is_datetime64_any_dtype(series.dtype):
        if not values.map(lambda value: isinstance(value, (date, datetime)) or bool(_DATE_VALUE.fullmatch(str(value)))).all():
            return None
    dates = pd.to_datetime(series, format="mixed", errors="coerce", utc=True)
    return dates.dt.tz_convert(None) if dates.notna().sum() == len(values) else None


def _json_value(value: Any) -> str | int | float | bool | None:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating, Decimal)):
        number = float(value)
        return round(number, 6) if np.isfinite(number) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str):
        return "[redacted]" if _SENSITIVE_VALUE.search(value) else value[:160]
    return "[unsupported value]"


def _numeric_summary(series: pd.Series) -> dict:
    quartiles = series.quantile([0.25, 0.5, 0.75])
    iqr = quartiles.loc[0.75] - quartiles.loc[0.25]
    outliers = (series < quartiles.loc[0.25] - 1.5 * iqr) | (series > quartiles.loc[0.75] + 1.5 * iqr)
    return {
        "count": len(series),
        "min": _json_value(series.min()),
        "max": _json_value(series.max()),
        "sum": _json_value(series.sum()),
        "mean": _json_value(series.mean()),
        "median": _json_value(quartiles.loc[0.5]),
        "stddev": _json_value(series.std()),
        "iqr_outlier_count": int(outliers.sum()) if len(series) >= 4 else None,
    }


def _temporal_summary(dates: pd.Series, measure: pd.Series) -> dict | None:
    paired = pd.DataFrame({"date": dates, "value": measure}).dropna().sort_values("date")
    if len(paired) < 2 or paired["date"].duplicated().any():
        return None
    first, last = paired.iloc[0], paired.iloc[-1]
    peak = paired.iloc[paired["value"].to_numpy().argmax()]
    baseline, latest = float(first.value), float(last.value)
    return {
        "period_count": len(paired),
        "first_date": first.date.isoformat(),
        "last_date": last.date.isoformat(),
        "first_value": _json_value(baseline),
        "last_value": _json_value(latest),
        "peak_date": peak.date.isoformat(),
        "peak_value": _json_value(peak.value),
        "absolute_change": _json_value(latest - baseline),
        "percent_change": _json_value(100 * (latest - baseline) / baseline) if baseline > 0 else None,
    }


def build_evidence(frame: pd.DataFrame, *, truncated: bool = False, sample_rows: int = 8) -> dict:
    """Describe returned rows, omitting raw values in aggregate-only mode."""
    columns = _columns(frame)
    evidence: dict[str, Any] = {"row_count": len(frame), "truncated": truncated, "columns": []}
    profiled = dict(list(columns.items())[:32])
    if len(profiled) < len(frame.columns):
        evidence["unprofiled_columns"] = len(frame.columns) - len(profiled)
    dates: dict[str, pd.Series] = {}
    safe: list[str] = []
    numeric: dict[str, pd.Series] = {}
    for name, series in profiled.items():
        protected = bool(_SENSITIVE.search(_normalized(name))) or _identifier(name)
        evidence["columns"].append(
            {"name": name, "dtype": str(series.dtype), "missing": int(series.isna().sum()), "values_withheld": protected}
        )
        if protected:
            continue
        safe.append(name)
        numbers = _numbers(series)
        if numbers is not None and numbers.notna().any():
            numeric[name] = numbers
            continue
        parsed = _dates(series)
        if parsed is not None:
            dates[name] = parsed

    measures = dict(list(numeric.items())[:12])
    if measures:
        evidence["numeric_summary"] = {name: _numeric_summary(values.dropna()) for name, values in measures.items()}
    if dates:
        date_name, date_values = next(iter(dates.items()))
        trends = {name: _temporal_summary(date_values, values) for name, values in measures.items()}
        present = {name: trend for name, trend in trends.items() if trend is not None}
        if present:
            evidence["temporal_summary"] = {"date_column": date_name, "measures": present}
    if sample_rows > 0 and safe and not frame.empty:
        indexes = np.linspace(0, len(frame) - 1, min(sample_rows, len(frame), 12), dtype=int)
        evidence["sample"] = [{name: _json_value(columns[name].iloc[index]) for name in safe[:12]} for index in indexes]
    return evidence


def _recommended_chart(
    columns: dict[str, pd.Series], recommendation: Mapping[str, Any] | ChartSpec | None
) -> ChartSpec | None:
    if isinstance(recommendation, ChartSpec):
        chosen = recommendation
    elif isinstance(recommendation, Mapping):
        kind, x, y = (recommendation.get(key) for key in ("type", "x", "y"))
        if not isinstance(kind, str) or not isinstance(x, str) or (y is not None and not isinstance(y, str)):
            return None
        color, title, currency = (recommendation.get(key) for key in ("color", "title", "currency"))
        chosen = ChartSpec(
            kind.lower(), x, y, color if isinstance(color, str) else None,
            title if isinstance(title, str) else None, currency if currency in ("$", "€", "£", "₹") else None,
        )
    else:
        return None
    if chosen.kind not in _KINDS or chosen.x not in columns:
        return None
    if chosen.kind == "pie":
        chosen = replace(chosen, color=None)
    if chosen.color is not None and chosen.color not in columns:
        return None
    if chosen.color is not None and _categories(columns[chosen.color]).nunique() > 12:
        return None
    if chosen.kind == "histogram":
        if _numbers(columns[chosen.x]) is None:
            return None
        return replace(chosen, y=None)
    if chosen.y not in columns or chosen.x == chosen.y:
        return None
    if chosen.kind == "scatter" and _numbers(columns[chosen.x]) is None and _dates(columns[chosen.x]) is None:
        return None
    measure = _numbers(columns[chosen.y])
    if measure is None or not measure.notna().any():
        return None
    if chosen.kind == "pie" and (measure.min() < 0 or measure.max() <= 0):
        return None
    return chosen


def _manual_chart(
    columns: dict[str, pd.Series], kind: str, suggested: ChartSpec | None, measures: list[str], dates: list[str]
) -> ChartSpec | None:
    if kind not in _KINDS or not measures:
        return None
    if suggested is not None:
        candidate = replace(suggested, kind=kind)
        if kind == "histogram":
            candidate = replace(candidate, x=suggested.y or suggested.x, y=None)
        valid = _recommended_chart(columns, candidate)
        if valid is not None:
            return valid

    if kind == "histogram":
        return ChartSpec(kind, measures[0])
    dimensions = [name for name, values in columns.items() if name not in measures and not _identifier(name)]
    axes = measures[1:] + dates if kind == "scatter" else dates + dimensions + measures[1:]
    for axis in dict.fromkeys(axes):
        for measure in measures:
            candidate = _recommended_chart(columns, ChartSpec(kind, axis, measure))
            if candidate is not None:
                return candidate
    return None


def choose_chart(
    frame: pd.DataFrame, recommendation: Mapping[str, Any] | ChartSpec | None = None, *, kind: str | None = None
) -> ChartSpec | None:
    if frame.empty:
        return None
    columns = _columns(frame)
    explicit = _recommended_chart(columns, recommendation)
    if kind is None and explicit is not None:
        return explicit

    measures = [name for name, values in columns.items() if not _identifier(name) and _numbers(values) is not None]
    if not measures:
        return None
    dates = [name for name, values in columns.items() if _dates(values) is not None]
    if kind is not None:
        return _manual_chart(columns, kind, explicit, measures, dates)
    if len(frame) < 2:
        return None
    if dates:
        return ChartSpec("line", dates[0], measures[0])
    for name, values in columns.items():
        if name in measures or _identifier(name):
            continue
        distinct = values.dropna().astype(str).nunique()
        if 2 <= distinct <= min(30, len(frame)) and distinct >= len(frame) / 2:
            return ChartSpec("bar", name, measures[0])
    if len(measures) >= 2:
        return ChartSpec("scatter", measures[0], measures[1])
    return ChartSpec("histogram", measures[0])


def _label(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def _categories(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("Missing").map(lambda value: escape(value[:200]))


def _trace_values(series: pd.Series) -> tuple[pd.Series, str]:
    dates = _dates(series)
    if dates is not None:
        return dates, "date"
    numbers = _numbers(series)
    if numbers is not None:
        return numbers, "linear"
    return _categories(series), "category"


def build_chart(
    frame: pd.DataFrame, recommendation: Mapping[str, Any] | ChartSpec | None = None, title: str | None = None,
    *, kind: str | None = None,
):
    """Build a responsive Plotly figure from supported result columns."""
    import plotly.graph_objects as go

    spec = choose_chart(frame, recommendation, kind=kind)
    if spec is None:
        return None
    if not frame.index.is_unique:
        frame = frame.reset_index(drop=True)
    columns = _columns(frame)
    x, axis_type = _trace_values(columns[spec.x])
    y = _numbers(columns[spec.y]) if spec.y is not None else None
    fig = go.Figure()
    x_label = escape(_label(spec.x))
    y_label = escape(_label(spec.y)) if spec.y is not None else "Count"
    prefix = spec.currency or ""
    monthly = axis_type == "date" and x.dropna().dt.day.eq(1).all()
    date_format = "|%b %Y" if monthly else "|%d %b %Y" if axis_type == "date" else ""
    value_format = ",.2f" if prefix else ",.5~g"
    hover = f"%{{x{date_format}}}<br>{y_label}: {prefix}%{{y:{value_format}}}<extra>%{{fullData.name}}</extra>"

    if spec.kind == "pie":
        fig.add_trace(
            go.Pie(labels=_categories(columns[spec.x]), values=y, hole=0.62, sort=False,
                   marker={"colors": _COLORS}, textinfo="percent", hovertemplate="%{label}<br>%{value:,}<extra></extra>")
        )
    else:
        if spec.color is None:
            groups = [(y_label, frame.index)]
        else:
            labels = _categories(columns[spec.color])
            groups = labels.groupby(labels, sort=False).groups.items()
        for index, (name, group) in enumerate(groups):
            color = _COLORS[index % len(_COLORS)]
            group_x = x.loc[group]
            group_y = y.loc[group] if y is not None else None
            if spec.kind in {"line", "bar"} and axis_type == "date":
                order = group_x.sort_values().index
                group_x = group_x.loc[order]
                group_y = group_y.loc[order]
            if spec.kind == "histogram":
                trace = go.Histogram(x=group_x, name=name, marker_color=color, opacity=0.84)
            elif spec.kind == "bar":
                trace = go.Bar(x=group_x, y=group_y, name=name, marker_color=color, hovertemplate=hover)
            else:
                marker = {"color": color, "size": 8 if spec.kind == "scatter" else 6}
                trace = go.Scatter(
                    x=group_x, y=group_y, name=name, mode="lines+markers" if spec.kind == "line" else "markers",
                    marker=marker, line={"color": color, "width": 3}, hovertemplate=hover,
                )
            fig.add_trace(trace)

    chart_title = title if title is not None else spec.title
    fig.update_layout(
        template="plotly_dark", autosize=True, height=345,
        title={"text": escape(chart_title), "x": 0.01, "font": {"size": 17}} if chart_title else None,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 8, "r": 12, "t": 44 if chart_title else 10, "b": 10},
        font={"family": "Inter, Arial, sans-serif", "color": "#96a8bd", "size": 11},
        legend={"orientation": "h", "y": 1.06, "x": 0.01},
        hoverlabel={"bgcolor": "#172a42", "bordercolor": "#33465e", "font_color": "#f0f6ff"},
        showlegend=spec.color is not None or spec.kind == "pie", bargap=0.38, barmode="group",
    )
    if spec.kind != "pie":
        date_ticks = None
        if monthly:
            date_ticks = "%b" if x.dropna().dt.year.nunique() == 1 else "%b %y"
        fig.update_xaxes(
            type=axis_type, tickformat=date_ticks, showgrid=False, zeroline=False, automargin=True,
            tickprefix=prefix if spec.kind == "histogram" else "",
            title_text=x_label if spec.kind in {"scatter", "histogram"} else None,
        )
        fig.update_yaxes(
            tickprefix=prefix if spec.kind != "histogram" else "", tickformat="~s", gridcolor="#25364b", griddash="dot",
            zeroline=False, automargin=True, rangemode="tozero" if spec.kind == "bar" else "normal",
            title_text=y_label if spec.kind in {"scatter", "histogram"} else None,
        )
    return fig
