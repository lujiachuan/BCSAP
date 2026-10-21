"""手动控制设备库与主面板编排对话框。"""

from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class _CardEditor(QDialog):
    def __init__(
        self, card: dict, devices: list[dict], unavailable: set[str], parent=None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑主面板卡片")
        self.resize(460, 520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.title_edit = QLineEdit(str(card.get("title") or ""))
        form.addRow("卡片名称", self.title_edit)
        layout.addLayout(form)
        layout.addWidget(QLabel("选择这张卡片包含的设备："))
        self.device_list = QListWidget()
        selected = set(card.get("device_ids") or [])
        for device in devices:
            device_id = str(device["device_id"])
            item = QListWidgetItem(f"{device['label']}　{device_id}")
            item.setData(Qt.ItemDataRole.UserRole, device_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if device_id in selected else Qt.CheckState.Unchecked
            )
            if device_id in unavailable:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip("该设备已属于其他卡片")
            self.device_list.addItem(item)
        layout.addWidget(self.device_list, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_checked)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_checked(self) -> None:
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "无法保存", "卡片名称不能为空。")
            return
        if not self.device_ids():
            QMessageBox.warning(self, "无法保存", "一张设备卡片至少需要一个设备。")
            return
        self.accept()

    def device_ids(self) -> list[str]:
        return [
            str(self.device_list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.device_list.count())
            if self.device_list.item(row).checkState() == Qt.CheckState.Checked
        ]


class DeviceLibraryDialog(QDialog):
    """查看设备库，并编排哪些卡片显示在手动控制主面板。"""

    def __init__(self, config: dict, devices: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设备库与主面板")
        self.resize(680, 560)
        self._version = int(config.get("version") or 1)
        self._devices = list(devices)

        layout = QVBoxLayout(self)
        note = QLabel(
            "勾选决定是否显示在主面板；卡片改名、排序或删除不会修改PV映射、设备锁和安全边界。"
        )
        note.setWordWrap(True)
        note.setObjectName("mutedText")
        layout.addWidget(note)
        self.card_list = QListWidget()
        self.card_list.itemDoubleClicked.connect(lambda _item: self._edit())
        layout.addWidget(self.card_list, 1)
        cards = sorted(
            config.get("cards") or [], key=lambda item: item.get("display_order", 0)
        )
        for card in cards:
            self._append(dict(card))

        actions = QHBoxLayout()
        for text, slot in (
            ("新增卡片", self._add),
            ("编辑", self._edit),
            ("删除", self._delete),
            ("上移", lambda: self._move(-1)),
            ("下移", lambda: self._move(1)),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _append(self, card: dict) -> None:
        item = QListWidgetItem()
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            Qt.CheckState.Checked if card.get("visible", True) else Qt.CheckState.Unchecked
        )
        item.setData(Qt.ItemDataRole.UserRole, card)
        self._refresh_item(item)
        self.card_list.addItem(item)

    @staticmethod
    def _refresh_item(item: QListWidgetItem) -> None:
        card = dict(item.data(Qt.ItemDataRole.UserRole) or {})
        suffix = "系统卡片" if card.get("system") else f"{len(card.get('device_ids') or [])} 个设备"
        item.setText(f"{card.get('title') or '未命名'}　｜　{suffix}")

    def _used_devices(self, except_item: QListWidgetItem | None = None) -> set[str]:
        used: set[str] = set()
        for row in range(self.card_list.count()):
            item = self.card_list.item(row)
            if item is except_item:
                continue
            used.update((item.data(Qt.ItemDataRole.UserRole) or {}).get("device_ids") or [])
        return used

    def _add(self) -> None:
        card = {
            "card_id": f"card.{uuid4().hex}",
            "title": "新设备卡片",
            "device_ids": [],
            "visible": True,
            "display_order": self.card_list.count(),
            "system": False,
        }
        editor = _CardEditor(card, self._devices, self._used_devices(), self)
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        card["title"] = editor.title_edit.text().strip()
        card["device_ids"] = editor.device_ids()
        self._append(card)
        self.card_list.setCurrentRow(self.card_list.count() - 1)

    def _edit(self) -> None:
        item = self.card_list.currentItem()
        if item is None:
            return
        card = dict(item.data(Qt.ItemDataRole.UserRole) or {})
        if card.get("system"):
            QMessageBox.information(self, "系统卡片", "系统趋势卡片只允许显示、隐藏和排序。")
            return
        editor = _CardEditor(card, self._devices, self._used_devices(item), self)
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        card["title"] = editor.title_edit.text().strip()
        card["device_ids"] = editor.device_ids()
        item.setData(Qt.ItemDataRole.UserRole, card)
        self._refresh_item(item)

    def _delete(self) -> None:
        row = self.card_list.currentRow()
        if row < 0:
            return
        item = self.card_list.item(row)
        card = item.data(Qt.ItemDataRole.UserRole) or {}
        if card.get("system"):
            QMessageBox.information(self, "系统卡片", "系统趋势卡片不能删除，可以取消勾选隐藏。")
            return
        self.card_list.takeItem(row)

    def _move(self, offset: int) -> None:
        row = self.card_list.currentRow()
        target = row + offset
        if row < 0 or target < 0 or target >= self.card_list.count():
            return
        item = self.card_list.takeItem(row)
        self.card_list.insertItem(target, item)
        self.card_list.setCurrentRow(target)

    def config(self) -> dict:
        cards: list[dict] = []
        for row in range(self.card_list.count()):
            item = self.card_list.item(row)
            card = dict(item.data(Qt.ItemDataRole.UserRole) or {})
            card["visible"] = item.checkState() == Qt.CheckState.Checked
            card["display_order"] = row
            cards.append(card)
        return {"version": self._version, "cards": cards}
