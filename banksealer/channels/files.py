"""File and database channels: CSV/JSON-lines file (with follow mode) and SQLite table polling."""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3

from .base import Channel


class CSVChannel(Channel):
    """Reads a CSV (or .jsonl) file; with ``follow`` it keeps reading lines appended later."""

    type_name = "csv"
    description = "CSV / JSON-lines file, optionally followed like 'tail -f'"
    default_options = {"path": "data/incoming.csv", "follow": True, "poll_seconds": 1.0, "delimiter": ","}

    def run(self) -> None:
        path = self.options["path"]
        poll = float(self.options.get("poll_seconds", 1.0))
        follow = str(self.options.get("follow", True)).lower() not in ("false", "0", "no")
        while not os.path.exists(path):
            if self._stop.wait(poll):
                return
        jsonl = path.endswith(".jsonl")
        with open(path, "r", encoding="utf-8", newline="") as fh:
            header = None
            if not jsonl:
                first = fh.readline()
                while not first and not self._stop.is_set():
                    self._stop.wait(poll)
                    first = fh.readline()
                header = next(csv.reader([first], delimiter=self.options.get("delimiter", ",")))
            partial = ""
            while not self._stop.is_set():
                line = fh.readline()
                if not line:
                    if not follow:
                        break
                    self._stop.wait(poll)
                    continue
                if not line.endswith("\n"):   # writer has not finished the line yet
                    partial += line
                    continue
                line, partial = partial + line, ""
                if not line.strip():
                    continue
                if jsonl:
                    try:
                        self.emit(json.loads(line))
                    except json.JSONDecodeError as exc:
                        self.last_error = f"invalid JSON line: {exc}"
                else:
                    values = next(csv.reader(io.StringIO(line), delimiter=self.options.get("delimiter", ",")))
                    self.emit(dict(zip(header, values)))


class SQLiteChannel(Channel):
    """Polls a table of an SQLite database for rows newer than the last one read."""

    type_name = "sqlite"
    description = "Poll an SQLite table (e.g. a core-banking export)"
    default_options = {"path": "data/core_bank.db", "table": "transactions", "poll_seconds": 2.0, "batch": 1000}

    def run(self) -> None:
        path, table = self.options["path"], self.options["table"]
        if not table.replace("_", "").isalnum():
            raise ValueError(f"invalid table name {table!r}")
        poll = float(self.options.get("poll_seconds", 2.0))
        last = int(self.options.get("start_rowid", 0))
        while not self._stop.is_set():
            if os.path.exists(path):
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute(f"SELECT rowid AS _rowid_, * FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?",
                                        (last, int(self.options.get("batch", 1000)))).fetchall()
                except sqlite3.OperationalError as exc:  # table not there yet
                    self.last_error = str(exc)
                    rows = []
                finally:
                    conn.close()
                for row in rows:
                    record = dict(row)
                    last = int(record.pop("_rowid_"))
                    record.pop("rowid", None)
                    self.emit(record)
                if rows:
                    continue
            self._stop.wait(poll)
