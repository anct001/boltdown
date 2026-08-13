"""Queues and their schedules - IDM's "Scheduler" window.

One queue holds one schedule (start time, optional stop time, weekdays) and
one action to run when it drains. The dialog only edits rows; deciding when
anything fires is `QueueScheduler`'s job.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QTime, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLineEdit,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.schedule import (
    DAY_NAMES,
    EVERY_DAY,
    PostAction,
    Schedule,
    days_of,
    mask_for,
    parse_hhmm,
)
from ..util.fmt import human_size, parse_size
from ..storage.db import Database
from .controller import Controller
from .i18n import tr
from .scheduler import schedule_from_row

QUEUE_ROLE = Qt.ItemDataRole.UserRole + 1


def _hhmm(value: str | None, fallback: tuple[int, int]) -> tuple[int, int]:
    parsed = parse_hhmm(value)
    return (parsed.hour, parsed.minute) if parsed else fallback

ACTIONS: list[tuple[str, PostAction]] = [
    ("Do nothing", PostAction.NONE),
    ("Exit Boltdown", PostAction.EXIT),
    ("Shut down", PostAction.SHUTDOWN),
    ("Hibernate", PostAction.HIBERNATE),
    ("Sleep", PostAction.SLEEP),
]


class SchedulerDialog(QDialog):
    def __init__(self, controller: Controller, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.db = db
        self.setWindowTitle(tr("Scheduler"))
        self.setMinimumWidth(620)

        self.queue_list = QListWidget()
        self.queue_list.currentItemChanged.connect(self._on_queue_selected)

        new = QPushButton(tr("New queue"))
        new.clicked.connect(self.create_queue)
        rename = QPushButton(tr("Rename"))
        rename.clicked.connect(self.rename_queue)
        delete = QPushButton(tr("Delete"))
        delete.clicked.connect(self.delete_queue)
        list_buttons = QHBoxLayout()
        for button in (new, rename, delete):
            list_buttons.addWidget(button)

        left = QVBoxLayout()
        left.addWidget(QLabel(tr("Queues")))
        left.addWidget(self.queue_list, 1)
        left.addLayout(list_buttons)

        self.panel = self._build_panel()

        body = QHBoxLayout()
        body.addLayout(left, 1)
        right = QVBoxLayout()
        right.addWidget(self.panel)
        right.addWidget(self._build_bandwidth())
        right.addStretch(1)
        body.addLayout(right, 2)

        self.start_button = QPushButton(tr("Start now"))
        self.start_button.clicked.connect(self.start_queue)
        self.stop_button = QPushButton(tr("Stop"))
        self.stop_button.clicked.connect(self.stop_queue)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.addButton(self.start_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.stop_button, QDialogButtonBox.ButtonRole.ActionRole)
        save = buttons.addButton(
            tr("Save"), QDialogButtonBox.ButtonRole.ApplyRole
        )
        save.clicked.connect(self.save_current)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addWidget(buttons)

        controller.queuesChanged.connect(self.reload)
        self.reload()

    # ------------------------------------------------------------------ build

    def _build_panel(self) -> QWidget:
        panel = QGroupBox(tr("Schedule"))
        self.enabled = QCheckBox(tr("Start this queue automatically"))
        self.start_time = QTimeEdit(QTime(2, 0))
        self.start_time.setDisplayFormat("HH:mm")
        self.use_stop = QCheckBox(tr("Stop at:"))
        self.stop_time = QTimeEdit(QTime(6, 0))
        self.stop_time.setDisplayFormat("HH:mm")
        self.use_stop.toggled.connect(self.stop_time.setEnabled)
        self.stop_time.setEnabled(False)

        self.days = [QCheckBox(tr(name)) for name in DAY_NAMES]
        days_row = QHBoxLayout()
        for box in self.days:
            box.setChecked(True)
            days_row.addWidget(box)

        self.concurrent = QSpinBox()
        self.concurrent.setRange(1, 10)
        self.on_complete = QComboBox()
        for label, action in ACTIONS:
            self.on_complete.addItem(tr(label), action.value)

        self.next_run = QLabel("-")
        self.next_run.setEnabled(False)

        stop_row = QHBoxLayout()
        stop_row.addWidget(self.use_stop)
        stop_row.addWidget(self.stop_time)
        stop_row.addStretch(1)

        grid = QGridLayout(panel)
        grid.addWidget(self.enabled, 0, 0, 1, 2)
        grid.addWidget(QLabel(tr("Start at:")), 1, 0)
        grid.addWidget(self.start_time, 1, 1)
        grid.addLayout(stop_row, 2, 0, 1, 2)
        grid.addWidget(QLabel(tr("Days:")), 3, 0)
        grid.addLayout(days_row, 3, 1)
        grid.addWidget(QLabel(tr("Files at once:")), 4, 0)
        grid.addWidget(self.concurrent, 4, 1)
        grid.addWidget(QLabel(tr("When finished:")), 5, 0)
        grid.addWidget(self.on_complete, 5, 1)
        grid.addWidget(QLabel(tr("Next run:")), 6, 0)
        grid.addWidget(self.next_run, 6, 1)
        return panel

    def _build_bandwidth(self) -> QWidget:
        """A global "go slow between these hours" rule, separate from queues."""
        box = QGroupBox(tr("Bandwidth window"))
        saved = self.controller.settings.get("bandwidth_schedule") or {}
        self.bw_enabled = QCheckBox(tr("Limit the speed between:"))
        self.bw_enabled.setChecked(bool(saved.get("enabled")))
        self.bw_start = QTimeEdit(QTime(*_hhmm(saved.get("start"), (8, 0))))
        self.bw_start.setDisplayFormat("HH:mm")
        self.bw_stop = QTimeEdit(QTime(*_hhmm(saved.get("stop"), (18, 0))))
        self.bw_stop.setDisplayFormat("HH:mm")
        self.bw_limit = QLineEdit(
            human_size(saved["limit"]).replace(" ", "") if saved.get("limit") else ""
        )
        self.bw_limit.setPlaceholderText(tr("unlimited"))

        row = QHBoxLayout()
        row.addWidget(self.bw_start)
        row.addWidget(QLabel("-"))
        row.addWidget(self.bw_stop)
        row.addStretch(1)

        grid = QGridLayout(box)
        grid.addWidget(self.bw_enabled, 0, 0, 1, 2)
        grid.addLayout(row, 1, 0, 1, 2)
        grid.addWidget(QLabel(tr("Speed limit:")), 2, 0)
        grid.addWidget(self.bw_limit, 2, 1)
        note = QLabel(tr("Outside the window your normal limit comes back."))
        note.setWordWrap(True)
        note.setEnabled(False)
        grid.addWidget(note, 3, 0, 1, 2)
        return box

    def save_bandwidth(self) -> None:
        try:
            limit = parse_size(self.bw_limit.text().strip()) if self.bw_limit.text().strip() else None
        except ValueError:
            QMessageBox.warning(self, tr("Scheduler"), tr("Speed limit:"))
            return
        self.controller.settings.set("bandwidth_schedule", {
            "enabled": self.bw_enabled.isChecked(),
            "start": self.bw_start.time().toString("HH:mm"),
            "stop": self.bw_stop.time().toString("HH:mm"),
            "limit": limit,
            "days": EVERY_DAY,
        })

    # ----------------------------------------------------------------- state

    def reload(self) -> None:
        current = self.current_queue_id()
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        for info in self.controller.queues():
            waiting = len(self.controller.queue_items(info.id))
            label = f"{info.name}  ({waiting})"
            if info.running:
                label += "  " + tr("running")
            entry = QListWidgetItem(label)
            entry.setData(QUEUE_ROLE, info.id)
            self.queue_list.addItem(entry)
        self.queue_list.blockSignals(False)

        if self.queue_list.count():
            row = max(0, self._row_of(current))
            self.queue_list.setCurrentRow(row)
        else:
            self.panel.setEnabled(False)
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)

    def _row_of(self, queue_id: int | None) -> int:
        for row in range(self.queue_list.count()):
            if self.queue_list.item(row).data(QUEUE_ROLE) == queue_id:
                return row
        return 0

    def current_queue_id(self) -> int | None:
        entry = self.queue_list.currentItem()
        return entry.data(QUEUE_ROLE) if entry is not None else None

    def _on_queue_selected(self, current: QListWidgetItem | None, _previous=None) -> None:
        queue_id = current.data(QUEUE_ROLE) if current is not None else None
        self.panel.setEnabled(queue_id is not None)
        if queue_id is None:
            return
        info = self.controller.queue(queue_id)
        row = self.db.get_schedule(queue_id)
        schedule = schedule_from_row(row) if row is not None else Schedule(queue_id=queue_id)
        self._load(schedule, info.max_concurrent if info else 1)
        running = bool(info and info.running)
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def _load(self, schedule: Schedule, max_concurrent: int) -> None:
        self.enabled.setChecked(bool(schedule.enabled and schedule.start_at))
        start = schedule.start_time
        if start is not None:
            self.start_time.setTime(QTime(start.hour, start.minute))
        stop = schedule.stop_time
        self.use_stop.setChecked(stop is not None)
        self.stop_time.setEnabled(stop is not None)
        if stop is not None:
            self.stop_time.setTime(QTime(stop.hour, stop.minute))
        selected = days_of(schedule.days_mask or EVERY_DAY)
        for day, box in enumerate(self.days):
            box.setChecked(day in selected)
        self.concurrent.setValue(max(1, max_concurrent))
        index = self.on_complete.findData(schedule.action.value)
        self.on_complete.setCurrentIndex(max(0, index))

        upcoming = schedule.next_start(datetime.now()) if schedule.enabled else None
        self.next_run.setText(
            upcoming.strftime("%d/%m/%Y %H:%M") if upcoming else tr("not scheduled")
        )

    # ---------------------------------------------------------------- actions

    def create_queue(self) -> None:
        name, ok = QInputDialog.getText(self, tr("New queue"), tr("Name:"))
        name = name.strip()
        if not ok or not name:
            return
        if any(q.name == name for q in self.controller.queues()):
            QMessageBox.warning(self, tr("New queue"), tr("That name is already used."))
            return
        queue_id = self.controller.create_queue(name)
        self.reload()
        self.queue_list.setCurrentRow(self._row_of(queue_id))

    def rename_queue(self) -> None:
        queue_id = self.current_queue_id()
        if queue_id is None:
            return
        info = self.controller.queue(queue_id)
        name, ok = QInputDialog.getText(
            self, tr("Rename"), tr("Name:"), text=info.name if info else ""
        )
        if ok and name.strip():
            self.controller.update_queue(queue_id, name=name.strip())
            self.reload()

    def delete_queue(self) -> None:
        queue_id = self.current_queue_id()
        if queue_id is None:
            return
        confirm = QMessageBox.question(
            self, tr("Delete"), tr("Delete this queue? The downloads stay in the list.")
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.controller.delete_queue(queue_id)
        self.db.delete_schedule(queue_id)
        self.reload()

    def start_queue(self) -> None:
        queue_id = self.current_queue_id()
        if queue_id is not None:
            self.controller.start_queue(queue_id)
            self.reload()

    def stop_queue(self) -> None:
        queue_id = self.current_queue_id()
        if queue_id is not None:
            self.controller.stop_queue(queue_id)
            self.reload()

    def save_current(self) -> None:
        self.save_bandwidth()
        queue_id = self.current_queue_id()
        if queue_id is None:
            return
        # Read the whole form first: saving the queue emits `queuesChanged`,
        # which reloads the list and would repopulate these widgets from the
        # database halfway through.
        mask = mask_for([day for day, box in enumerate(self.days) if box.isChecked()])
        start_at = self.start_time.time().toString("HH:mm")
        stop_at = (
            self.stop_time.time().toString("HH:mm") if self.use_stop.isChecked() else None
        )
        enabled = self.enabled.isChecked()
        on_complete = self.on_complete.currentData()
        previous = self.db.get_schedule(queue_id)

        self.controller.update_queue(queue_id, max_concurrent=self.concurrent.value())
        # Changing the time makes this a different occurrence: forget the last
        # run so an earlier "already fired today" does not block it.
        last_run = None
        if previous is not None and previous["start_at"] == start_at:
            last_run = previous["last_run"]
        self.db.save_schedule(
            queue_id,
            start_at=start_at,
            stop_at=stop_at,
            days_mask=mask,
            enabled=enabled,
            on_complete=on_complete,
            last_run=last_run,
        )
        self._on_queue_selected(self.queue_list.currentItem())
