"""Small in-memory cache of date projections; never a second pricing store."""

from collections import OrderedDict
from datetime import date
from pathlib import Path


class DateViewCache:
    def __init__(self, database: Path, choices_directory: Path, limit: int = 4):
        self.database = Path(database)
        self.choices_directory = Path(choices_directory)
        self.limit = max(1, limit)
        self._stamp = None
        self._views = OrderedDict()

    @staticmethod
    def _file_stamp(path):
        try:
            info = path.stat()
            return (str(path), info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            return (str(path), None, None)

    def revision(self):
        # WAL matters when another planner/editor instance writes to SQLite.
        files = [self.database, Path(str(self.database) + "-wal")]
        files.extend(sorted(self.choices_directory.glob("*.json")))
        return (date.today().isoformat(), tuple(self._file_stamp(p) for p in files))

    def clear(self):
        self._views.clear()
        self._stamp = None

    def get(self, key_date):
        stamp = self.revision()
        if stamp != self._stamp:
            self.clear()
            self._stamp = stamp
        value = self._views.get(key_date)
        if value is None:
            return None
        self._views.move_to_end(key_date)
        view, baseline, warning = value
        return view.copy(deep=True), baseline.copy(deep=True), warning

    def put(self, key_date, view, baseline, warning, *, revision):
        if self.revision() != revision:
            # A write during projection must not become a reusable snapshot.
            self.clear()
            return
        if self._stamp != revision:
            self.clear()
            self._stamp = revision
        self._views[key_date] = (view.copy(deep=True), baseline.copy(deep=True), warning)
        self._views.move_to_end(key_date)
        while len(self._views) > self.limit:
            self._views.popitem(last=False)
