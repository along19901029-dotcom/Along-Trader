"""Monkey-patch an agent module so datetime.now() returns historical dates.

Uses a MockDatetime class that overrides now(). The agent's `from datetime import datetime`
gets replaced with MockDatetime in the module namespace.
"""
import time as _time
from datetime import datetime as _real_datetime


class MockDatetime(_real_datetime):
    """Subclass of datetime that overrides now() to return a frozen time."""
    _frozen = None

    def __new__(cls, *args, **kwargs):
        # When called as datetime(year, month, day, ...), delegate to real
        if args:
            return _real_datetime.__new__(_real_datetime, *args, **kwargs)
        return _real_datetime.__new__(_real_datetime, *args, **kwargs)

    @classmethod
    def now(cls, tz=None):
        if cls._frozen is not None:
            if tz is not None and cls._frozen.tzinfo is not None:
                return cls._frozen.astimezone(tz)
            return cls._frozen
        return _real_datetime.now(tz)


class TimeMachine:
    """Context manager to freeze time for an agent module."""

    def __init__(self, module):
        self._module = module
        self._orig_datetime = getattr(module, "datetime", _real_datetime)
        self._orig_time = _time.time
        self._orig_sleep = _time.sleep
        self._active = False

    def set(self, dt):
        """Freeze time to given datetime."""
        MockDatetime._frozen = dt

    def __enter__(self):
        if not self._active:
            self._module.datetime = MockDatetime
            _time.time = self._time_fn
            _time.sleep = self._sleep_fn
            self._active = True
        return self

    def __exit__(self, *args):
        self._module.datetime = self._orig_datetime
        _time.time = self._orig_time
        _time.sleep = self._orig_sleep
        self._active = False
        MockDatetime._frozen = None

    def _time_fn(self):
        if MockDatetime._frozen is not None:
            return MockDatetime._frozen.timestamp()
        return self._orig_time()

    def _sleep_fn(self, seconds):
        pass
