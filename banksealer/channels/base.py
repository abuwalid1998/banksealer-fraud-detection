"""Channel abstraction: a source of raw transaction records running in a background thread."""
from __future__ import annotations

import queue
import threading
from typing import Optional

from ..preparation import TransactionPreparer


class Channel:
    """Base class. Subclasses implement :meth:`run` and call :meth:`emit` for each raw record.

    Records go to a shared queue as ``(channel_name, record, is_history)``; preparation happens
    in the consumer thread with this channel's :class:`TransactionPreparer` (own field mapping
    and statistics).
    """

    type_name = "base"
    description = ""
    default_options: dict = {}

    def __init__(self, name: str, options: Optional[dict] = None, field_mapping: Optional[dict] = None,
                 out_queue: Optional[queue.Queue] = None, preparation: Optional[dict] = None):
        self.name = name
        self.options = {**self.default_options, **(options or {})}
        self.field_mapping = dict(field_mapping or {})
        self.preparation = dict(preparation or {})
        self.queue: queue.Queue = out_queue if out_queue is not None else queue.Queue()
        self.preparer = TransactionPreparer(self.field_mapping, default_bank=self.options.get("default_bank", "DEFAULT"),
                                            rules=self.preparation)
        self.status = "stopped"
        self.last_error = ""
        self.received = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.status = "starting"
        self._thread = threading.Thread(target=self._safe_run, name=f"channel-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.on_stop()
        if self._thread:
            self._thread.join(timeout=3)
        self.status = "stopped"

    def on_stop(self) -> None:
        """Hook to unblock servers (close sockets...)."""

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _safe_run(self) -> None:
        try:
            self.status = "running"
            self.run()
            if self.status == "running":
                self.status = "finished" if not self._stop.is_set() else "stopped"
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.status = "error"
            self.last_error = f"{type(exc).__name__}: {exc}"

    def run(self) -> None:
        raise NotImplementedError

    # --------------------------------------------------------------- data
    def emit(self, record: dict, history: bool = False) -> None:
        self.received += 1
        self.queue.put((self.name, record, history))

    def describe(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in self.options.items())
