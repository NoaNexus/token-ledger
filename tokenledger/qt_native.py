from __future__ import annotations

import argparse
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from PySide6.QtCore import QObject, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .analytics import build_dashboard
from .config import default_data_dir, default_user_home
from .db import TokenDatabase
from .native import APP_NAME, APP_VERSION, compact_number, metric_rows, migrate_legacy_database, percent, ratio_percent
from .providers import AntigravityAdapter, ClaudeAdapter, CodexAdapter
from .scanner import ScanCoordinator


CANVAS = "#F5F5F7"
SIDEBAR = "#FBFBFD"
SURFACE = "#FFFFFF"
INK = "#1D1D1F"
MUTED = "#6E6E73"
FAINT = "#73737A"
LINE = "#E5E5EA"
BLUE = "#0066CC"
GREEN = "#34C759"
ORANGE = "#FF9F0A"
VIOLET = "#AF52DE"
RED = "#FF3B30"
AGENT_COLORS = {"codex": BLUE, "claude": ORANGE, "antigravity": VIOLET}
STATUS_LABELS = {
    "ready": "可用",
    "partial": "部分可用",
    "limited": "部分可用",
    "empty": "暂无记录",
    "missing": "未发现",
    "pending": "等待扫描",
    "scanning": "扫描中",
    "stale": "已过期",
    "error": "异常",
    "unavailable": "未提供",
    "budget": "本地预算",
}


def resource_path(relative: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return root / relative


def _set_margins(layout: QVBoxLayout | QHBoxLayout | QGridLayout, *values: int) -> None:
    layout.setContentsMargins(*values)


def _clear_layout(layout: QVBoxLayout | QHBoxLayout | QGridLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            widget.hide()
            widget.deleteLater()
        elif child_layout is not None:
            _clear_layout(child_layout)  # type: ignore[arg-type]


def _label(text: str, role: str = "body", parent: QWidget | None = None) -> QLabel:
    widget = QLabel(text, parent)
    widget.setProperty("role", role)
    return widget


def _pixel_font(size: int, weight: QFont.Weight = QFont.Normal, family: str = "Microsoft YaHei UI") -> QFont:
    font = QFont(family)
    font.setPixelSize(size)
    font.setWeight(weight)
    font.setHintingPreference(QFont.PreferFullHinting)
    return font


def _card(parent: QWidget | None = None, name: str = "card") -> QFrame:
    widget = QFrame(parent)
    widget.setObjectName(name)
    return widget


def _format_time(value: Any) -> str:
    if not value:
        return "未提供"
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return text[:16].replace("T", " ")


def _safe_token(metrics: dict[str, Any], key: str, available: bool = True) -> str:
    if not available:
        return "未提供"
    return compact_number(metrics.get(key))


def _paint_cache_signature(widget: QWidget) -> tuple[int, int, float]:
    """Describe the logical size and monitor scale used by a widget paint cache."""

    return widget.width(), widget.height(), round(max(widget.devicePixelRatioF(), 1.0), 3)


def _create_hidpi_pixmap(widget: QWidget, background: str) -> QPixmap:
    """Create a pixmap whose backing pixels match the widget's current monitor."""

    scale = max(widget.devicePixelRatioF(), 1.0)
    pixmap = QPixmap(
        max(1, round(widget.width() * scale)),
        max(1, round(widget.height() * scale)),
    )
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(QColor(background))
    return pixmap


def _initial_window_geometry(available: QRect) -> QRect:
    """Fit the restored window inside the taskbar-safe desktop work area."""

    width = min(1440, max(720, available.width() - 48), available.width())
    height = min(900, max(480, available.height() - 72), available.height())
    x = available.x() + max((available.width() - width) // 2, 0)
    y = available.y() + max((available.height() - height) // 2, 0)
    return QRect(x, y, width, height)


class UiBridge(QObject):
    dashboard_ready = Signal(int, object)
    dashboard_error = Signal(str)


class MetricCard(QFrame):
    def __init__(self, title: str, accent: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("metricCard")
        self.setProperty("accent", accent)
        self.setMinimumWidth(140)
        self.setMinimumHeight(132)
        layout = QVBoxLayout(self)
        _set_margins(layout, 18, 17, 18, 17)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        marker = QFrame()
        marker.setFixedSize(6, 6)
        marker.setStyleSheet(f"background:{accent}; border-radius:3px;")
        top.addWidget(marker)
        top.addWidget(_label(title, "metricLabel"))
        top.addStretch(1)
        layout.addLayout(top)
        self.value = _label("—", "metricValue")
        self.note = _label("等待本机数据", "metricNote")
        self.note.setWordWrap(True)
        layout.addWidget(self.value)
        layout.addWidget(self.note)

    def set_data(self, value: str, note: str) -> None:
        if self.value.text() != value:
            self.value.setText(value)
        if self.note.text() != note:
            self.note.setText(note)


class SmoothScrollArea(QScrollArea):
    """Qt-native wheel handling for mouse wheels and precision touchpads."""

    def wheelEvent(self, event: Any) -> None:
        super().wheelEvent(event)


class TokenFlowWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(170)
        self._values: dict[str, int] = {}
        self._paint_cache: QPixmap | None = None
        self._paint_cache_signature: tuple[int, int, float] | None = None

    def set_data(self, metrics: dict[str, Any]) -> None:
        values = {
            "input": int(metrics.get("input") or 0),
            "cached": int(metrics.get("cached_input") or 0),
            "output": int(metrics.get("output") or 0),
            "reasoning": int(metrics.get("reasoning") or 0),
        }
        if values == self._values:
            return
        self._values = values
        self.setAccessibleName(
            "Token 流向："
            + "，".join(
                f"{name} {compact_number(value)}"
                for name, value in (
                    ("输入", self._values["input"]),
                    ("缓存读取", self._values["cached"]),
                    ("输出", self._values["output"]),
                    ("推理", self._values["reasoning"]),
                )
            )
        )
        self._paint_cache = None
        self._paint_cache_signature = None
        self.update()

    def paintEvent(self, _event: Any) -> None:
        signature = _paint_cache_signature(self)
        if self._paint_cache is None or self._paint_cache_signature != signature:
            self._paint_cache = _create_hidpi_pixmap(self, "#F7FBFF")
            self._paint_cache_signature = signature
            cache_painter = QPainter(self._paint_cache)
            self._paint_content(cache_painter)
            cache_painter.end()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._paint_cache)

    def resizeEvent(self, event: Any) -> None:
        self._paint_cache = None
        self._paint_cache_signature = None
        super().resizeEvent(event)

    def _paint_content(self, painter: QPainter) -> None:
        painter.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        width = self.width()
        left, right = 18.0, 18.0
        available = max(width - left - right, 320.0)
        items = (
            ("输入", self._values.get("input", 0), BLUE),
            ("缓存读取", self._values.get("cached", 0), GREEN),
            ("输出", self._values.get("output", 0), ORANGE),
            ("推理", self._values.get("reasoning", 0), VIOLET),
        )
        centers = [left + available * (index + 0.5) / len(items) for index in range(len(items))]
        rail_y = 116.0
        painter.setPen(QPen(QColor("#D9E2EE"), 3))
        painter.drawLine(QPointF(centers[0], rail_y), QPointF(centers[-1], rail_y))
        label_font = _pixel_font(13)
        value_font = _pixel_font(23, QFont.DemiBold)
        max_value = max((item[1] for item in items), default=0) or 1
        for index, (name, value, color) in enumerate(items):
            x = centers[index]
            painter.setFont(label_font)
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(x - available / 9, 13, available / 4.5, 24), Qt.AlignCenter, name)
            painter.setFont(value_font)
            painter.setPen(QColor(INK))
            painter.drawText(QRectF(x - available / 8, 42, available / 4, 36), Qt.AlignCenter, compact_number(value))
            radius = 7.0 + 4.0 * (value / max_value)
            painter.setPen(QPen(QColor(SURFACE), 3))
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(x, rail_y), radius, radius)
            if index < len(items) - 1:
                start = x + radius + 5
                end = centers[index + 1] - 12
                if end > start:
                    gradient = QLinearGradient(start, rail_y, end, rail_y)
                    gradient.setColorAt(0, QColor(color))
                    gradient.setColorAt(1, QColor(items[index + 1][2]))
                    painter.setPen(QPen(gradient, 3))
                    painter.drawLine(QPointF(start, rail_y), QPointF(end, rail_y))
        painter.setFont(_pixel_font(12))
        painter.setPen(QColor(FAINT))
        painter.drawText(QRectF(left, 143, available, 20), Qt.AlignCenter, "推理 Token 已包含在输出中，不重复计入总量")


class TrendChart(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(280)
        self._daily: list[dict[str, Any]] = []
        self._paint_cache: QPixmap | None = None
        self._paint_cache_signature: tuple[int, int, float] | None = None

    def set_data(self, daily: Iterable[dict[str, Any]]) -> None:
        updated = list(daily)
        if updated == self._daily:
            return
        self._daily = updated
        self._paint_cache = None
        self._paint_cache_signature = None
        self.update()

    def paintEvent(self, _event: Any) -> None:
        signature = _paint_cache_signature(self)
        if self._paint_cache is None or self._paint_cache_signature != signature:
            self._paint_cache = _create_hidpi_pixmap(self, SURFACE)
            self._paint_cache_signature = signature
            cache_painter = QPainter(self._paint_cache)
            self._paint_content(cache_painter)
            cache_painter.end()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._paint_cache)

    def resizeEvent(self, event: Any) -> None:
        self._paint_cache = None
        self._paint_cache_signature = None
        super().resizeEvent(event)

    def _paint_content(self, painter: QPainter) -> None:
        painter.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        width, height = self.width(), self.height()
        left, right, top, bottom = 58.0, 18.0, 18.0, 34.0
        chart_w = max(width - left - right, 10.0)
        chart_h = max(height - top - bottom, 10.0)
        values = [max(int(item.get("total") or 0), 0) for item in self._daily]
        maximum = max(values, default=0)
        painter.setFont(_pixel_font(12))
        for index in range(4):
            y = top + chart_h * index / 3
            painter.setPen(QPen(QColor("#E7EBF2"), 1, Qt.DashLine))
            painter.drawLine(QPointF(left, y), QPointF(width - right, y))
            label_value = maximum * (3 - index) / 3 if maximum else 0
            painter.setPen(QColor(FAINT))
            painter.drawText(QRectF(0, y - 10, left - 8, 20), Qt.AlignRight | Qt.AlignVCenter, compact_number(label_value))
        if not values or maximum <= 0:
            painter.setFont(_pixel_font(14))
            painter.setPen(QColor(FAINT))
            painter.drawText(self.rect(), Qt.AlignCenter, "当前范围没有可绘制的 Token 用量")
            return
        denominator = max(len(values) - 1, 1)
        points = [
            QPointF(left + chart_w * index / denominator, top + chart_h - value / maximum * chart_h)
            for index, value in enumerate(values)
        ]
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        area = QPainterPath(path)
        area.lineTo(QPointF(points[-1].x(), top + chart_h))
        area.lineTo(QPointF(points[0].x(), top + chart_h))
        area.closeSubpath()
        gradient = QLinearGradient(0, top, 0, top + chart_h)
        gradient_start = QColor(BLUE)
        gradient_start.setAlpha(64)
        gradient_end = QColor(BLUE)
        gradient_end.setAlpha(0)
        gradient.setColorAt(0, gradient_start)
        gradient.setColorAt(1, gradient_end)
        painter.fillPath(area, gradient)
        painter.setPen(QPen(QColor(BLUE), 2.5))
        painter.drawPath(path)
        step = max(len(points) // 6, 1)
        for index, point in enumerate(points):
            if index % step and index != len(points) - 1:
                continue
            painter.setPen(QPen(QColor(SURFACE), 2))
            painter.setBrush(QColor(BLUE))
            painter.drawEllipse(point, 4, 4)
            painter.setPen(QColor(FAINT))
            painter.setFont(_pixel_font(12))
            date_text = str(self._daily[index].get("date") or "")[-5:]
            painter.drawText(QRectF(point.x() - 28, height - 25, 56, 18), Qt.AlignCenter, date_text)


class QuotaRow(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        _set_margins(layout, 0, 0, 0, 0)
        layout.setSpacing(5)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.title = _label("额度未提供", "quotaTitle")
        self.value = _label("—", "quotaValue")
        row.addWidget(self.title)
        row.addStretch(1)
        row.addWidget(self.value)
        layout.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setObjectName("quotaBar")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setRange(0, 1000)
        layout.addWidget(self.bar)
        self.note = _label("没有可靠的服务端额度来源", "quotaNote")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

    def set_data(self, quota: dict[str, Any] | None) -> None:
        if not quota or quota.get("status") == "unavailable":
            self.title.setText((quota or {}).get("label") or "额度未提供")
            self.value.setText("—")
            self.note.setText((quota or {}).get("message") or "没有可靠的服务端额度来源")
            self.bar.setValue(0)
            self.bar.setProperty("state", "unknown")
            self.bar.setToolTip("额度未提供")
            self.bar.setAccessibleName("额度未提供")
            return
        remaining = quota.get("remaining_percent")
        used = quota.get("used_percent")
        if used is None and remaining is not None:
            used = 100.0 - float(remaining)
        if remaining is None and used is not None:
            remaining = 100.0 - float(used)
        remaining_value = max(0.0, min(float(remaining or 0), 100.0))
        used_value = max(0.0, min(float(used or 0), 100.0))
        title = str(quota.get("label") or "额度窗口")
        if quota.get("status") == "budget":
            title = f"{title} · 本地预算"
        self.title.setText(title)
        self.value.setText(f"剩余 {percent(remaining)}" if remaining is not None else "额度未知")
        # 蓝色表示剩余额度；QProgressBar 从左侧填充，未填充的灰色区域会随使用量
        # 从右向左扩展。这样 100% 剩余时是完整蓝条，符合额度余额的直觉。
        self.bar.setValue(int(remaining_value * 10))
        quota_hint = f"蓝色为剩余 {percent(remaining_value)}，灰色为已用 {percent(used_value)}"
        self.bar.setToolTip(quota_hint)
        self.bar.setAccessibleName(f"{title}：{quota_hint}")
        state = "stale" if quota.get("status") == "stale" else "ready"
        self.bar.setProperty("state", state)
        notes = []
        if quota.get("resets_at"):
            notes.append(f"重置 {_format_time(quota.get('resets_at'))}")
        if quota.get("message"):
            notes.append(str(quota["message"]))
        self.note.setText(" · ".join(notes) or f"更新 {_format_time(quota.get('updated_at'))}")


class AgentCard(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("agentCard")
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.layout_root = QVBoxLayout(self)
        _set_margins(self.layout_root, 22, 20, 22, 20)
        self.layout_root.setSpacing(14)

    def set_data(self, agent: dict[str, Any], lifetime: dict[str, Any]) -> None:
        _clear_layout(self.layout_root)
        color = AGENT_COLORS.get(str(agent.get("id")), BLUE)
        header = QHBoxLayout()
        dot = QFrame()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet(f"background:{color}; border-radius:4px;")
        header.addWidget(dot)
        header.addWidget(_label(str(agent.get("name") or "未知 Agent"), "agentTitle"))
        header.addStretch(1)
        status = _label(STATUS_LABELS.get(str(agent.get("status")), str(agent.get("status") or "未知")), "statusPill")
        status.setProperty("status", str(agent.get("status") or "pending"))
        header.addWidget(status)
        self.layout_root.addLayout(header)

        usage_available = bool(agent.get("usage_available", True))
        total_text = _safe_token(agent, "total", usage_available)
        total = _label(total_text, "agentTotal")
        total.setAccessibleName(f"{agent.get('name')} 当前范围总 Token {total_text}")
        self.layout_root.addWidget(total)
        range_note = "当前范围"
        if agent.get("contains_estimates"):
            range_note += f" · 含估算 {compact_number(agent.get('estimated_total'))}"
        if not usage_available:
            range_note = "本地记录未提供可统计的 Token"
        self.layout_root.addWidget(_label(range_note, "agentNote"))

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(12)
        metrics.setVerticalSpacing(9)
        for index, (name, value) in enumerate(metric_rows(agent)):
            block = QWidget()
            block_layout = QVBoxLayout(block)
            _set_margins(block_layout, 0, 0, 0, 0)
            block_layout.setSpacing(2)
            block_layout.addWidget(_label(name, "microLabel"))
            shown = value if usage_available else "未提供"
            block_layout.addWidget(_label(shown, "microValue"))
            metrics.addWidget(block, index // 3, index % 3)
        self.layout_root.addLayout(metrics)

        cache_row = QHBoxLayout()
        cache = ratio_percent(agent.get("cache_hit_rate")) if usage_available else None
        cache_row.addWidget(_label("缓存命中率", "microLabel"))
        cache_row.addStretch(1)
        cache_row.addWidget(_label(percent(cache), "cacheValue"))
        self.layout_root.addLayout(cache_row)
        cache_bar = QProgressBar()
        cache_bar.setObjectName("cacheBar")
        cache_bar.setTextVisible(False)
        cache_bar.setFixedHeight(6)
        cache_bar.setRange(0, 1000)
        cache_bar.setValue(int((cache or 0) * 10))
        cache_bar.setStyleSheet(f"QProgressBar::chunk {{ background:{color}; border-radius:3px; }}")
        self.layout_root.addWidget(cache_bar)

        meta = QHBoxLayout()
        session_suffix = "已知会话" if agent.get("session_count_complete") is False else "会话"
        meta.addWidget(_label(f"{int(agent.get('sessions') or 0):,} {session_suffix}", "agentMeta"))
        meta.addWidget(_label(f"{int(agent.get('models') or 0):,} 个模型", "agentMeta"))
        meta.addWidget(_label(f"{int(agent.get('calls') or 0):,} 条记录", "agentMeta"))
        meta.addStretch(1)
        self.layout_root.addLayout(meta)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        self.layout_root.addWidget(divider)
        self.layout_root.addWidget(_label("额度与窗口", "sectionMini"))
        windows = list(agent.get("quota_windows") or [])
        if not windows and agent.get("quota"):
            windows = [agent["quota"]]
        if not windows:
            windows = [agent.get("quota")]
        for quota in windows[:2]:
            row = QuotaRow()
            row.set_data(quota)
            self.layout_root.addWidget(row)

        reconciliation = agent.get("reconciliation") or {}
        if reconciliation.get("has_account_rollup"):
            text = (
                f"CC Switch 账户汇总覆盖 {int(reconciliation.get('account_days') or 0)} 天；"
                f"已抑制 {int(reconciliation.get('suppressed_session_events') or 0)} 条重叠会话记录"
            )
            note = _label(text, "reconcileNote")
            note.setWordWrap(True)
            self.layout_root.addWidget(note)
        lifetime_total = lifetime.get("total")
        self.layout_root.addWidget(_label(f"全部历史 {compact_number(lifetime_total)} Token", "lifetimeNote"))


class SourceCard(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sourceCard")
        self.layout_root = QVBoxLayout(self)
        _set_margins(self.layout_root, 22, 20, 22, 20)
        self.layout_root.setSpacing(12)

    def set_data(self, source: dict[str, Any]) -> None:
        _clear_layout(self.layout_root)
        top = QHBoxLayout()
        top.addWidget(_label(str(source.get("label") or source.get("agent") or "数据源"), "sourceTitle"))
        top.addStretch(1)
        status = _label(STATUS_LABELS.get(str(source.get("status")), str(source.get("status") or "未知")), "statusPill")
        status.setProperty("status", str(source.get("status") or "pending"))
        top.addWidget(status)
        self.layout_root.addLayout(top)
        path = _label(str(source.get("path_hint") or "等待发现本地路径"), "sourcePath")
        path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path.setWordWrap(True)
        self.layout_root.addWidget(path)
        counts = QHBoxLayout()
        for title, value in (
            ("文件", int(source.get("files") or 0)),
            ("记录", int(source.get("events") or 0)),
            ("会话", int(source.get("sessions") or 0)),
        ):
            counts.addWidget(_label(f"{title}  {value:,}", "sourceCount"))
        counts.addStretch(1)
        counts.addWidget(_label(f"扫描 {_format_time(source.get('last_scan'))}", "sourceCount"))
        self.layout_root.addLayout(counts)
        message = _label(str(source.get("message") or "等待首次扫描"), "sourceMessage")
        message.setWordWrap(True)
        self.layout_root.addWidget(message)
        metadata = source.get("metadata") or {}
        details: list[str] = []
        if metadata.get("current_platform"):
            details.append(f"当前平台：{metadata['current_platform']}")
        if metadata.get("current_route"):
            details.append(f"路由：{metadata['current_route']}")
        if metadata.get("account_rollup"):
            details.append("已接入 CC Switch 账户日汇总")
        if metadata.get("usage_mode") == "estimated":
            details.append(f"本地估算：{compact_number(metadata.get('estimated_tokens'))} Token")
        if metadata.get("activity_count"):
            details.append(f"活动记录：{int(metadata['activity_count']):,}")
        if metadata.get("truncated_entries"):
            details.append(f"截断记录：{int(metadata['truncated_entries']):,}")
        if details:
            detail = _label("   ·   ".join(details), "sourceDetail")
            detail.setWordWrap(True)
            self.layout_root.addWidget(detail)


class MainWindow(QMainWindow):
    def __init__(self, user_home: Path, data_dir: Path, timezone_name: str) -> None:
        super().__init__()
        self.user_home = user_home
        self.data_dir = data_dir
        self.timezone_name = timezone_name
        self.database_path = data_dir / "token-ledger.db"
        self.migrated_from = migrate_legacy_database(self.database_path)
        self.database = TokenDatabase(self.database_path)
        self.adapters = [CodexAdapter(user_home), ClaudeAdapter(user_home), AntigravityAdapter(user_home)]
        self.scanner = ScanCoordinator(self.database, self.adapters)
        self.range_value: int | None = 30
        self.agent_value = "all"
        self.generation = 0
        self.last_scan_state = "idle"
        self.closing = False
        self._scan_display: tuple[str, str] | None = None
        self._model_render_key: str | None = None
        self._agent_render_key: str | None = None
        self._source_render_key: str | None = None
        self.bridge = UiBridge()
        self.bridge.dashboard_ready.connect(self.apply_dashboard)
        self.bridge.dashboard_error.connect(self.show_error)
        self.range_buttons: dict[int | None, QPushButton] = {}
        self.nav_buttons: dict[str, QPushButton] = {}
        self.metric_cards: dict[str, MetricCard] = {}
        self._agent_cards: list[AgentCard] = []
        self._build_window()
        self._build_ui()
        self.request_dashboard()
        QTimer.singleShot(500, self.start_scan)
        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self.poll_scan)
        self.scan_timer.start(600)
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self.auto_scan)
        self.auto_timer.start(60_000)

    def _build_window(self) -> None:
        self.setWindowTitle(f"{APP_NAME} · 本机 AI 用量")
        icon_path = resource_path("assets/token-ledger.ico")
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))
        screen = QApplication.primaryScreen()
        if screen is not None:
            geometry = _initial_window_geometry(screen.availableGeometry())
            self.setMinimumSize(min(1080, geometry.width()), min(640, geometry.height()))
            self.setGeometry(geometry)
        else:
            self.setMinimumSize(960, 600)
            self.resize(1280, 800)
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TokenLedger.Native.2")
            except Exception:
                pass

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("appRoot")
        root = QHBoxLayout(central)
        _set_margins(root, 0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        body = QWidget()
        body_layout = QVBoxLayout(body)
        _set_margins(body_layout, 0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self._build_topbar())
        self.stack = QStackedWidget()
        self.overview_page = self._build_overview_page()
        self.sources_page = self._build_sources_page()
        self.stack.addWidget(self.overview_page)
        self.stack.addWidget(self.sources_page)
        body_layout.addWidget(self.stack, 1)
        root.addWidget(body, 1)
        self.setCentralWidget(central)
        self.set_page("overview")

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(224)
        layout = QVBoxLayout(sidebar)
        _set_margins(layout, 20, 24, 20, 20)
        layout.setSpacing(7)
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setObjectName("brandMark")
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(38, 38)
        application_icon = QApplication.windowIcon()
        if application_icon.isNull():
            logo.setText("T")
        else:
            logo.setPixmap(application_icon.pixmap(38, 38))
        brand.addWidget(logo)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_text.addWidget(_label("Token 账本", "brandTitle"))
        brand_text.addWidget(_label("个人 AI 用量", "brandCaption"))
        brand.addLayout(brand_text)
        layout.addLayout(brand)
        layout.addSpacing(30)
        layout.addWidget(_label("浏览", "navSection"))
        for key, title in (
            ("overview", "用量总览"),
            ("sources", "数据源诊断"),
        ):
            button = QPushButton(title)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, page=key: self.set_page(page))
            layout.addWidget(button)
            self.nav_buttons[key] = button
        layout.addStretch(1)
        privacy = _label("本地读取 · 不保存对话正文\n不上传 Token 数据", "privacyNote")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        layout.addSpacing(12)
        layout.addWidget(_label(f"VERSION {APP_VERSION}", "versionLabel"))
        return sidebar

    def _build_topbar(self) -> QWidget:
        topbar = QFrame()
        topbar.setObjectName("topbar")
        layout = QHBoxLayout(topbar)
        _set_margins(layout, 30, 17, 30, 17)
        layout.setSpacing(13)
        heading = QVBoxLayout()
        heading.setSpacing(1)
        self.page_title = _label("用量总览", "pageTitle")
        self.page_subtitle = _label("看清每个 Agent 的消耗、缓存与额度", "pageSubtitle")
        heading.addWidget(self.page_title)
        heading.addWidget(self.page_subtitle)
        layout.addLayout(heading)
        layout.addStretch(1)
        self.status_dot = QFrame()
        self.status_dot.setFixedSize(8, 8)
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setProperty("state", "ready")
        layout.addWidget(self.status_dot)
        self.status_label = _label("读取本机数据", "topStatus")
        layout.addWidget(self.status_label)
        self.scan_button = QPushButton("刷新数据")
        self.scan_button.setObjectName("primaryButton")
        self.scan_button.setCursor(Qt.PointingHandCursor)
        self.scan_button.clicked.connect(lambda: self.start_scan(True))
        layout.addWidget(self.scan_button)
        return topbar

    def _scroll_page(self) -> tuple[SmoothScrollArea, QWidget, QVBoxLayout]:
        scroll = SmoothScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("pageCanvas")
        layout = QVBoxLayout(content)
        _set_margins(layout, 30, 24, 30, 34)
        layout.setSpacing(18)
        scroll.setWidget(content)
        return scroll, content, layout

    def _build_overview_page(self) -> SmoothScrollArea:
        scroll, _content, layout = self._scroll_page()
        layout.addWidget(self._build_filters())

        summary_panel = _card(name="summaryPanel")
        metrics = QHBoxLayout(summary_panel)
        _set_margins(metrics, 6, 6, 6, 6)
        metrics.setSpacing(0)
        definitions = (
            ("lifetime", "累计 Token", BLUE),
            ("range", "当前范围", INK),
            ("net", "净用量", ORANGE),
            ("cache", "缓存命中", GREEN),
            ("sessions", "会话 / 记录", VIOLET),
        )
        for index, (key, title, color) in enumerate(definitions):
            card = MetricCard(title, color)
            metrics.addWidget(card, 1)
            self.metric_cards[key] = card
            if index < len(definitions) - 1:
                divider = QFrame()
                divider.setObjectName("metricDivider")
                divider.setFixedWidth(1)
                metrics.addWidget(divider)
        layout.addWidget(summary_panel)

        flow_card = _card(name="featureCard")
        flow_layout = QVBoxLayout(flow_card)
        _set_margins(flow_layout, 22, 20, 22, 16)
        flow_layout.setSpacing(6)
        header = QHBoxLayout()
        header.addWidget(_label("Token 流向", "sectionTitle"))
        header.addStretch(1)
        self.flow_note = _label("当前范围", "sectionNote")
        header.addWidget(self.flow_note)
        flow_layout.addLayout(header)
        self.token_flow = TokenFlowWidget()
        flow_layout.addWidget(self.token_flow)
        layout.addWidget(flow_card)

        chart_card = _card()
        chart_layout = QVBoxLayout(chart_card)
        _set_margins(chart_layout, 22, 20, 22, 18)
        chart_header = QHBoxLayout()
        chart_header.addWidget(_label("用量趋势", "sectionTitle"))
        chart_header.addStretch(1)
        self.chart_note = _label("按本地日期归集", "sectionNote")
        chart_header.addWidget(self.chart_note)
        chart_layout.addLayout(chart_header)
        self.trend_chart = TrendChart()
        chart_layout.addWidget(self.trend_chart)
        layout.addWidget(chart_card)

        models_card = _card()
        models_layout = QVBoxLayout(models_card)
        _set_margins(models_layout, 22, 20, 22, 20)
        models_header = QHBoxLayout()
        models_header.addWidget(_label("模型与路由", "sectionTitle"))
        models_header.addStretch(1)
        models_header.addWidget(_label("保留 Agent、平台、路由与统计口径", "sectionNote"))
        models_layout.addLayout(models_header)
        self.model_table = QTableWidget(0, 6)
        self.model_table.setObjectName("dataTable")
        self.model_table.setHorizontalHeaderLabels(("模型", "Agent", "平台 / 路由", "Token", "占比", "口径"))
        self.model_table.verticalHeader().hide()
        self.model_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.model_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.model_table.setShowGrid(False)
        self.model_table.setAlternatingRowColors(False)
        self.model_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.model_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.model_table.setFocusPolicy(Qt.NoFocus)
        self.model_table.setTextElideMode(Qt.ElideRight)
        self.model_table.setWordWrap(False)
        self.model_table.viewport().setAutoFillBackground(True)
        self.model_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.model_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.model_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.model_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.model_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.model_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        models_layout.addWidget(self.model_table)
        layout.addWidget(models_card)

        agents_heading = QHBoxLayout()
        agents_heading.addWidget(_label("Agent 账本", "sectionTitle"))
        agents_heading.addStretch(1)
        agents_heading.addWidget(_label("当前范围 / 全部历史 / 缓存 / 额度", "sectionNote"))
        layout.addLayout(agents_heading)
        self.agent_grid = QGridLayout()
        self.agent_grid.setHorizontalSpacing(14)
        self.agent_grid.setVerticalSpacing(14)
        layout.addLayout(self.agent_grid)
        layout.addStretch(1)
        return scroll

    def _build_filters(self) -> QFrame:
        filter_bar = QFrame()
        filter_bar.setObjectName("filterBar")
        filters = QHBoxLayout(filter_bar)
        _set_margins(filters, 12, 10, 12, 10)
        filters.setSpacing(10)
        filters.addWidget(_label("时间范围", "filterLabel"))
        group = QButtonGroup(self)
        group.setExclusive(True)
        segment_control = QFrame()
        segment_control.setObjectName("segmentControl")
        segment_layout = QHBoxLayout(segment_control)
        _set_margins(segment_layout, 3, 3, 3, 3)
        segment_layout.setSpacing(1)
        for value, title in ((1, "今天"), (7, "7 天"), (30, "30 天"), (None, "全部")):
            button = QPushButton(title)
            button.setObjectName("segmentButton")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, days=value: self.set_range(days))
            group.addButton(button)
            segment_layout.addWidget(button)
            self.range_buttons[value] = button
        filters.addWidget(segment_control)
        self.range_buttons[self.range_value].setChecked(True)
        filters.addSpacing(12)
        filters.addWidget(_label("Agent", "filterLabel"))
        self.agent_combo = QComboBox()
        self.agent_combo.setObjectName("agentCombo")
        self.agent_combo.addItem("全部 Agent", "all")
        self.agent_combo.addItem("Codex", "codex")
        self.agent_combo.addItem("Claude Code", "claude")
        self.agent_combo.addItem("Antigravity", "antigravity")
        self.agent_combo.currentIndexChanged.connect(self.set_agent)
        filters.addWidget(self.agent_combo)
        filters.addStretch(1)
        self.range_label = _label("最近 30 天", "rangeBadge")
        filters.addWidget(self.range_label)
        return filter_bar

    def _build_sources_page(self) -> SmoothScrollArea:
        scroll, _content, layout = self._scroll_page()
        intro = _card(name="featureCard")
        intro_layout = QHBoxLayout(intro)
        _set_margins(intro_layout, 22, 20, 22, 20)
        copy = QVBoxLayout()
        copy.addWidget(_label("本地数据源", "sectionTitle"))
        description = _label("展示扫描位置、记录覆盖与解析状态，不读取或展示对话正文、API Key 和登录凭据。", "bodyMuted")
        description.setWordWrap(True)
        copy.addWidget(description)
        intro_layout.addLayout(copy, 1)
        self.source_summary = _label("等待首次扫描", "rangeBadge")
        intro_layout.addWidget(self.source_summary)
        layout.addWidget(intro)
        self.sources_layout = QVBoxLayout()
        self.sources_layout.setSpacing(12)
        layout.addLayout(self.sources_layout)
        layout.addStretch(1)
        return scroll

    def set_page(self, page: str) -> None:
        index = 0 if page == "overview" else 1
        if hasattr(self, "stack"):
            self.stack.setCurrentIndex(index)
        for key, button in self.nav_buttons.items():
            button.setChecked(key == page)
        if hasattr(self, "page_title"):
            if page == "overview":
                self.page_title.setText("用量总览")
                self.page_subtitle.setText("看清每个 Agent 的消耗、缓存与额度")
            else:
                self.page_title.setText("数据源诊断")
                self.page_subtitle.setText("检查本机日志、账户汇总和估算数据是否完整")

    def set_range(self, value: int | None) -> None:
        if self.range_value == value:
            return
        self.range_value = value
        self.range_label.setText("全部历史" if value is None else ("今天" if value == 1 else f"最近 {value} 天"))
        self.request_dashboard()

    def set_agent(self, _index: int) -> None:
        self.agent_value = str(self.agent_combo.currentData() or "all")
        self.request_dashboard()

    def request_dashboard(self) -> None:
        self.generation += 1
        generation = self.generation
        selected = None if self.agent_value == "all" else self.agent_value
        scan_status = self.scanner.status()

        def worker() -> None:
            try:
                data = build_dashboard(self.database, scan_status, self.timezone_name, self.range_value, selected)
                self.bridge.dashboard_ready.emit(generation, data)
            except Exception as error:
                self.bridge.dashboard_error.emit(f"无法读取账本：{type(error).__name__}")

        threading.Thread(target=worker, daemon=True, name="token-ledger-qt-dashboard").start()

    def apply_dashboard(self, generation: int, data: object) -> None:
        if generation != self.generation or self.closing or not isinstance(data, dict):
            return
        summary = data["summary"]
        lifetime = data.get("lifetime", {})
        lifetime_summary = lifetime.get("summary", summary)
        range_meta = data["meta"]["range"]
        self.metric_cards["lifetime"].set_data(
            compact_number(lifetime_summary.get("total")),
            f"全部历史 · {int(lifetime_summary.get('total') or 0):,} Token",
        )
        range_note = f"{range_meta['start']} 至 {range_meta['end']}"
        if summary.get("contains_estimates"):
            range_note += f" · 含估算 {compact_number(summary.get('estimated_total'))}"
        self.metric_cards["range"].set_data(compact_number(summary.get("total")), range_note)
        self.metric_cards["net"].set_data(
            compact_number(summary.get("net_usage")),
            f"非缓存输入 {compact_number(summary.get('non_cached_input'))} + 输出",
        )
        cache = ratio_percent(summary.get("cache_hit_rate"))
        self.metric_cards["cache"].set_data(
            percent(cache),
            f"读取 {compact_number(summary.get('cached_input'))} · 写入 {compact_number(summary.get('cache_write'))}",
        )
        session_label = f"{int(summary.get('sessions') or 0):,} / {int(summary.get('calls') or 0):,}"
        session_note = "已知会话 / 账户记录" if summary.get("session_count_complete") is False else "会话 / 请求与记录"
        self.metric_cards["sessions"].set_data(session_label, session_note)
        self.flow_note.setText(f"{range_meta['days']} 天 · 净用量 {compact_number(summary.get('net_usage'))}")
        self.token_flow.set_data(summary)
        peak = max((int(item.get("total") or 0) for item in data.get("daily", [])), default=0)
        self.chart_note.setText(f"按本地日期归集 · 峰值 {compact_number(peak)}")
        self.trend_chart.set_data(data.get("daily", []))
        self._render_models(data.get("models", []))
        self._render_agents(data.get("agents", []), lifetime.get("agents", {}))
        self._render_sources(data.get("sources", []))

    def _render_models(self, models: list[dict[str, Any]]) -> None:
        rows = models[:12]
        render_key = repr(rows)
        if render_key == self._model_render_key:
            return
        self._model_render_key = render_key
        self.model_table.setUpdatesEnabled(False)
        row_height = max(44, self.model_table.fontMetrics().height() + 20)
        try:
            self.model_table.setRowCount(len(rows))
            agent_names = {"codex": "Codex", "claude": "Claude Code", "antigravity": "Antigravity"}
            for row, item in enumerate(rows):
                scope = "账户日汇总" if item.get("usage_scope") == "account_daily" else "会话"
                if item.get("usage_mode") == "estimated":
                    scope += " · 估算"
                route = " / ".join(value for value in (str(item.get("platform") or ""), str(item.get("route") or "")) if value)
                values = (
                    str(item.get("model") or "未识别模型"),
                    agent_names.get(str(item.get("agent")), str(item.get("agent") or "—")),
                    route or "平台未识别",
                    compact_number(item.get("total")),
                    percent(float(item.get("share") or 0) * 100),
                    scope,
                )
                for column, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    if column in {3, 4}:
                        cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    cell.setToolTip(value)
                    self.model_table.setItem(row, column, cell)
                self.model_table.setRowHeight(row, row_height)
        finally:
            self.model_table.setUpdatesEnabled(True)
            self.model_table.viewport().update()
        header_height = max(46, self.model_table.horizontalHeader().sizeHint().height())
        visible_height = header_height + row_height * max(len(rows), 1) + 2
        self.model_table.setFixedHeight(visible_height)

    def _render_agents(self, agents: list[dict[str, Any]], lifetime_agents: dict[str, Any]) -> None:
        visible = agents if self.agent_value == "all" else [item for item in agents if item.get("id") == self.agent_value]
        render_key = repr((self.agent_value, visible, lifetime_agents))
        if render_key == self._agent_render_key:
            return
        self._agent_render_key = render_key
        _clear_layout(self.agent_grid)
        self._agent_cards = []
        for agent in visible:
            card = AgentCard()
            card.set_data(agent, lifetime_agents.get(str(agent.get("id")), {}))
            self._agent_cards.append(card)
        self._layout_agent_cards()

    def _layout_agent_cards(self) -> None:
        if not hasattr(self, "agent_grid") or not self._agent_cards:
            return
        while self.agent_grid.count():
            self.agent_grid.takeAt(0)
        viewport_width = self.overview_page.viewport().width() if hasattr(self, "overview_page") else self.width() - 224
        content_width = max(viewport_width - 60, 0)
        columns = 3 if len(self._agent_cards) >= 3 and content_width >= 880 else min(2, len(self._agent_cards))
        for index, card in enumerate(self._agent_cards):
            self.agent_grid.addWidget(card, index // columns, index % columns)
        for column in range(3):
            self.agent_grid.setColumnStretch(column, 0)
        for column in range(columns):
            self.agent_grid.setColumnStretch(column, 1)

    def _render_sources(self, sources: list[dict[str, Any]]) -> None:
        render_key = repr(sources)
        if render_key == self._source_render_key:
            return
        self._source_render_key = render_key
        _clear_layout(self.sources_layout)
        ready = 0
        for source in sources:
            if source.get("status") in {"ready", "partial", "limited"}:
                ready += 1
            card = SourceCard()
            card.set_data(source)
            self.sources_layout.addWidget(card)
        self.source_summary.setText(f"{ready} / {len(sources)} 个来源可读取")

    def _set_scan_state(self, state: str, text: str) -> None:
        previous = self._scan_display
        if previous == (state, text):
            return
        self._scan_display = (state, text)
        if previous is None or previous[1] != text:
            self.status_label.setText(text)
        if previous is None or previous[0] != state:
            self.status_dot.setProperty("state", state)
            self.status_dot.style().unpolish(self.status_dot)
            self.status_dot.style().polish(self.status_dot)

    def start_scan(self, force: bool = False) -> None:
        if self.scanner.start_background(force=force):
            if self.scan_button.isEnabled():
                self.scan_button.setEnabled(False)
            self._set_scan_state("scanning", "正在扫描本机数据")

    def poll_scan(self) -> None:
        if self.closing:
            return
        status = self.scanner.status()
        state = str(status.get("status") or "idle")
        if state == "scanning":
            if self.scan_button.isEnabled():
                self.scan_button.setEnabled(False)
            self._set_scan_state("scanning", f"扫描 {int(status.get('progress') or 0)}%")
        elif state in {"ready", "partial"}:
            if not self.scan_button.isEnabled():
                self.scan_button.setEnabled(True)
            self._set_scan_state("ready" if state == "ready" else "warning", "本机数据已更新" if state == "ready" else "部分来源暂不可读")
        elif state == "error":
            if not self.scan_button.isEnabled():
                self.scan_button.setEnabled(True)
            self._set_scan_state("error", "扫描失败，请重试")
        if self.last_scan_state == "scanning" and state != "scanning":
            self.request_dashboard()
        self.last_scan_state = state

    def auto_scan(self) -> None:
        if not self.closing:
            self.scanner.start_background()

    def show_error(self, message: str) -> None:
        self._set_scan_state("error", message)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "agent_grid"):
            self._layout_agent_cards()

    def closeEvent(self, event: Any) -> None:
        self.closing = True
        super().closeEvent(event)


STYLE_SHEET = f"""
* {{ font-family: 'Microsoft YaHei UI'; color: {INK}; font-size: 12px; }}
QMainWindow, QWidget#appRoot, QWidget#pageCanvas {{ background: {CANVAS}; }}
QToolTip {{ background: {INK}; color: white; border: none; border-radius: 6px; padding: 6px 8px; }}
QFrame#sidebar {{ background: {SIDEBAR}; border: none; border-right: 1px solid {LINE}; }}
QLabel[role='brandTitle'] {{ color: {INK}; font-size: 16px; font-weight: 700; }}
QLabel[role='brandCaption'] {{ color: {MUTED}; font-size: 11px; }}
QLabel#brandMark {{ background: {BLUE}; color: white; border-radius: 10px; font-size: 19px; font-weight: 700; }}
QLabel[role='navSection'] {{ color: {FAINT}; font-size: 11px; font-weight: 600; padding: 0 12px 6px 12px; }}
QPushButton#navButton {{ background: transparent; color: #515154; border: none; border-radius: 10px; padding: 12px 14px; text-align: left; font-size: 13px; }}
QPushButton#navButton:hover {{ background: #F0F0F3; color: {INK}; }}
QPushButton#navButton:checked {{ background: #E8F2FF; color: {BLUE}; font-weight: 700; }}
QPushButton#navButton:focus {{ border: 1px solid #7FB3E8; padding: 11px 13px; }}
QLabel[role='privacyNote'] {{ color: {FAINT}; font-size: 11px; }}
QLabel[role='versionLabel'] {{ color: #8E8E93; font-size: 10px; }}
QFrame#topbar {{ background: {SIDEBAR}; border: none; border-bottom: 1px solid {LINE}; }}
QLabel[role='pageTitle'] {{ font-size: 24px; font-weight: 700; }}
QLabel[role='pageSubtitle'] {{ color: {MUTED}; font-size: 13px; }}
QLabel[role='topStatus'] {{ color: {MUTED}; font-size: 12px; }}
QFrame#statusDot {{ background: {GREEN}; border-radius: 4px; }}
QFrame#statusDot[state='scanning'] {{ background: {BLUE}; }}
QFrame#statusDot[state='warning'] {{ background: {ORANGE}; }}
QFrame#statusDot[state='error'] {{ background: {RED}; }}
QPushButton#primaryButton {{ background: {BLUE}; color: white; border: none; border-radius: 10px; padding: 10px 17px; font-size: 12px; font-weight: 700; }}
QPushButton#primaryButton:hover {{ background: #006EE6; }}
QPushButton#primaryButton:pressed {{ background: #005FC7; }}
QPushButton#primaryButton:disabled {{ background: #B7D7FF; color: white; }}
QPushButton#primaryButton:focus {{ border: 2px solid #8FC4F4; padding: 8px 15px; }}
QScrollArea#pageScroll {{ background: {CANVAS}; border: none; }}
QScrollBar:vertical {{ width: 9px; background: transparent; margin: 5px 2px; }}
QScrollBar::handle:vertical {{ background: #C7C7CC; border-radius: 4px; min-height: 40px; }}
QScrollBar::handle:vertical:hover {{ background: #AEAEB2; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QFrame#filterBar {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 14px; }}
QFrame#segmentControl {{ background: #F2F2F7; border: none; border-radius: 10px; }}
QLabel[role='filterLabel'] {{ color: {MUTED}; font-size: 12px; font-weight: 600; padding: 0 2px; }}
QPushButton#segmentButton {{ background: transparent; color: {MUTED}; border: 1px solid transparent; border-radius: 8px; padding: 7px 12px; font-size: 12px; }}
QPushButton#segmentButton:hover {{ color: {INK}; }}
QPushButton#segmentButton:checked {{ background: {SURFACE}; color: {INK}; border-color: #DEDEE3; font-weight: 700; }}
QPushButton#segmentButton:focus {{ border-color: #7FB3E8; }}
QComboBox#agentCombo {{ background: #F7F7FA; border: 1px solid #DEDEE3; border-radius: 9px; padding: 8px 36px 8px 12px; min-width: 142px; font-size: 12px; }}
QComboBox#agentCombo:focus {{ border: 2px solid #7FB3E8; padding: 7px 35px 7px 11px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {LINE}; selection-background-color: #E8F2FF; selection-color: {INK}; padding: 6px; }}
QLabel[role='rangeBadge'] {{ background: #F2F2F7; color: {MUTED}; border-radius: 9px; padding: 8px 11px; font-size: 12px; }}
QFrame#summaryPanel, QFrame#card, QFrame#agentCard, QFrame#sourceCard {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 18px; }}
QFrame#metricCard {{ background: transparent; border: none; }}
QFrame#metricDivider {{ background: {LINE}; border: none; margin-top: 14px; margin-bottom: 14px; }}
QFrame#featureCard {{ background: #F7FBFF; border: 1px solid #DCEBFF; border-radius: 18px; }}
QLabel[role='metricLabel'] {{ color: {MUTED}; font-size: 12px; }}
QLabel[role='metricValue'] {{ font-size: 28px; font-weight: 700; padding-top: 1px; }}
QLabel[role='metricNote'] {{ color: {FAINT}; font-size: 11px; }}
QLabel[role='sectionTitle'] {{ font-size: 18px; font-weight: 700; }}
QLabel[role='sectionNote'] {{ color: {FAINT}; font-size: 12px; }}
QTableWidget#dataTable {{ background: {SURFACE}; border: none; color: {INK}; alternate-background-color: #F8FAFC; selection-background-color: transparent; }}
QTableWidget#dataTable::item {{ border-bottom: 1px solid #EFEFF4; padding: 8px; font-size: 13px; }}
QHeaderView::section {{ background: #F7F7FA; color: {MUTED}; border: none; border-bottom: 1px solid {LINE}; padding: 10px 8px; font-size: 12px; font-weight: 700; }}
QLabel[role='agentTitle'], QLabel[role='sourceTitle'] {{ font-size: 16px; font-weight: 700; }}
QLabel[role='statusPill'] {{ background: #F2F2F7; color: {MUTED}; border-radius: 8px; padding: 5px 8px; font-size: 11px; }}
QLabel[role='statusPill'][status='ready'] {{ background: #EAF8EE; color: #248A3D; }}
QLabel[role='statusPill'][status='limited'], QLabel[role='statusPill'][status='partial'] {{ background: #FFF4E5; color: #B05A00; }}
QLabel[role='statusPill'][status='error'] {{ background: #FFEBEA; color: #D70015; }}
QLabel[role='agentTotal'] {{ font-size: 29px; font-weight: 700; }}
QLabel[role='agentNote'], QLabel[role='lifetimeNote'] {{ color: {FAINT}; font-size: 11px; }}
QLabel[role='microLabel'] {{ color: {MUTED}; font-size: 12px; }}
QLabel[role='microValue'] {{ font-size: 13px; font-weight: 700; }}
QLabel[role='cacheValue'] {{ color: #248A3D; font-size: 12px; font-weight: 700; }}
QProgressBar#cacheBar, QProgressBar#quotaBar {{ background: #E9E9EE; border: none; border-radius: 3px; }}
QProgressBar#quotaBar::chunk {{ background: {BLUE}; border-radius: 3px; }}
QProgressBar#quotaBar[state='unknown']::chunk {{ background: #C7C7CC; }}
QLabel[role='agentMeta'] {{ color: {MUTED}; background: #F5F5F7; border-radius: 7px; padding: 5px 7px; font-size: 11px; }}
QFrame#divider {{ background: {LINE}; border: none; }}
QLabel[role='sectionMini'] {{ color: {INK}; font-size: 12px; font-weight: 700; }}
QLabel[role='quotaTitle'] {{ color: {MUTED}; font-size: 11px; }}
QLabel[role='quotaValue'] {{ font-size: 11px; font-weight: 700; }}
QLabel[role='quotaNote'] {{ color: {FAINT}; font-size: 11px; }}
QLabel[role='reconcileNote'] {{ background: #FFF6E8; color: #8A4B08; border-radius: 9px; padding: 9px; font-size: 11px; }}
QLabel[role='bodyMuted'] {{ color: {MUTED}; font-size: 12px; }}
QLabel[role='sourcePath'] {{ color: #3A3A3C; background: #F5F5F7; border-radius: 9px; padding: 10px; font-family: 'Cascadia Mono'; font-size: 11px; }}
QLabel[role='sourceCount'] {{ color: {MUTED}; font-size: 11px; }}
QLabel[role='sourceMessage'] {{ color: {INK}; font-size: 12px; }}
QLabel[role='sourceDetail'] {{ color: {MUTED}; background: #F7F7FA; border-radius: 9px; padding: 9px; font-size: 11px; }}
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Token 账本 Windows Qt 桌面应用")
    parser.add_argument("--user-home", type=Path, default=default_user_home())
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--timezone", default="Asia/Shanghai")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    application = QApplication.instance() or QApplication([sys.argv[0]])
    application.setApplicationName(APP_NAME)
    application.setOrganizationName("TokenLedger")
    application.setStyle("Fusion")
    ui_font = QFont("Microsoft YaHei UI", 11)
    ui_font.setHintingPreference(QFont.PreferFullHinting)
    ui_font.setStyleStrategy(QFont.PreferAntialias)
    application.setFont(ui_font)
    application.setStyleSheet(STYLE_SHEET)
    icon_path = resource_path("assets/token-ledger.ico")
    if icon_path.is_file():
        application.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow(
        args.user_home.expanduser().resolve(),
        args.data_dir.expanduser().resolve(),
        args.timezone,
    )
    window.show()
    return application.exec()
