# -*- coding: utf-8 -*-
"""分片表格的数据模型：一片一行，状态带颜色。"""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont

from ..core import job
from ..core.api import human_size

COLUMNS = ("开始", "片", "时长", "码率", "体积", "预计输出", "节省", "状态")
RIGHT_ALIGNED = {1, 2, 3, 4, 5, 6}

# 运行中的步骤名是中文，直接按名字给色；其余是枚举值。
# 两套色：深色主题下浅色文字需要更亮的色相，否则在深底上看不清。
RUNNING_COLORS = {
    False: {"下载": "#2563eb", "试算": "#2563eb", "编码": "#7c3aed",
            "上传": "#0891b2", "校验": "#7c3aed"},
    True: {"下载": "#60a5fa", "试算": "#60a5fa", "编码": "#c4b5fd",
           "上传": "#22d3ee", "校验": "#c4b5fd"},
}
STATE_COLORS = {
    False: {"pending": ("待处理", "#6b7280"), "skip": ("跳过", "#9ca3af"),
            "estimated": ("已试算", "#0891b2"), "done": ("完成", "#15803d"),
            "failed": ("失败", "#b91c1c"), "stopped": ("已停止", "#a16207")},
    True: {"pending": ("待处理", "#9ca3af"), "skip": ("跳过", "#6b7280"),
           "estimated": ("已试算", "#22d3ee"), "done": ("完成", "#4ade80"),
           "failed": ("失败", "#f87171"), "stopped": ("已停止", "#fbbf24")},
}


def _status_style(status: str, dark: bool = False) -> tuple[str, str]:
    """返回 ``(显示名, 颜色)``；校验阶段的步骤名以「校验」开头。"""
    running = RUNNING_COLORS[dark]
    states = STATE_COLORS[dark]
    if status.startswith("校验"):
        return status, running["校验"]
    if status in running:
        return status, running[status]
    return states.get(status, (status, "#374151" if not dark else "#d1d5db"))


class PartsModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parts: list[job.Part] = []
        self.dark = False
        self._bold = QFont()
        self._bold.setBold(True)

    # ---------- Qt 接口 ----------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.parts)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            if orientation == Qt.Horizontal:
                return COLUMNS[section]
            return section + 1
        if role == Qt.TextAlignmentRole and orientation == Qt.Horizontal:
            # 与数据列对齐：数值靠右，文字靠左
            return int((Qt.AlignRight if section in RIGHT_ALIGNED else Qt.AlignLeft)
                       | Qt.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        part = self.parts[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            return self._text(part, col)
        if role == Qt.ForegroundRole and col == 7:
            return QColor(_status_style(part.status, self.dark)[1])
        if role == Qt.FontRole and col == 7:
            return self._bold
        if role == Qt.TextAlignmentRole and col in RIGHT_ALIGNED:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ToolTipRole:
            tip = part.title
            if part.message:
                tip += "\n" + part.message
            if part.path:
                tip += "\n" + part.path
            return tip
        return None

    def _text(self, part: job.Part, col: int) -> str:
        if col == 0:
            return part.when
        if col == 1:
            return str(part.idx + 1)
        if col == 2:
            secs = int(part.duration_ms / 1000)
            return "%d:%02d" % (secs // 60, secs % 60)
        if col == 3:
            return "%d kbps" % part.kbps if part.kbps else "—"
        if col == 4:
            return human_size(part.bytes)
        if col == 5:
            if part.est_bytes:
                return human_size(part.est_bytes)
            if part.status == "done":
                return human_size(part.bytes - part.saved)
            return "—"
        if col == 6:
            if part.status == "done" and part.saved:
                return human_size(part.saved)
            if part.est_bytes:
                return "%.0f%%" % (part.saving_ratio * 100)
            return "—"
        name, _ = _status_style(part.status, self.dark)
        # 结束状态带原因，运行中带百分比，两者都不占对方的位
        if part.message and part.status in ("skip", "done", "failed",
                                            "stopped", "estimated"):
            return "%s %s" % (name, part.message)
        if part.progress > 0 and part.status not in STATE_COLORS[self.dark]:
            return "%s %d%%" % (name, part.progress * 100)
        return name

    # ---------- 数据 ----------

    def set_parts(self, parts: list[job.Part]) -> None:
        self.beginResetModel()
        self.parts = list(parts)
        self.endResetModel()

    def refresh_row(self, row: int) -> None:
        if 0 <= row < len(self.parts):
            self.dataChanged.emit(self.index(row, 0),
                                  self.index(row, len(COLUMNS) - 1))

    def refresh_all(self) -> None:
        if self.parts:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self.parts) - 1, len(COLUMNS) - 1))

    def totals(self) -> tuple[int, int, int, int]:
        """返回 ``(片数, 源体积, 已省体积, 待处理片数)``。"""
        size = sum(p.bytes for p in self.parts)
        saved = sum(p.saved for p in self.parts)
        todo = sum(1 for p in self.parts if p.status not in ("skip", "done"))
        return len(self.parts), size, saved, todo
