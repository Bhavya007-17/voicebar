"""Bounded browser observations for one local Voicebar session.

These are observed accessibility identities, not a browser history database.
Only an exact, currently unique tab can be selected. Titles that change or
disappear lose their association; nothing is persisted to disk.
"""

import re
import threading
import time
from collections import OrderedDict


def normalized(value):
    return " ".join(str(value).split()).casefold()


def page_title(caption):
    """Remove a known browser caption suffix, never a partial page name."""
    return re.sub(r"\s+[-–—]\s+(?:Google Chrome|Microsoft Edge|Chromium)$", "",
                  str(caption), flags=re.IGNORECASE).strip()


def tab_identity(row):
    return normalized(row.get("name", "")), normalized(row.get("automation_id", ""))


def tab_selector(row):
    selector = {"name": row["name"], "role": "tabitem"}
    if row.get("automation_id"):
        selector["automation_id"] = row["automation_id"]
    return selector


def site_tab_matches(title, hint, hostname):
    """Match exact site labels in tab metadata, never fuzzy page prose."""
    title = normalized(title)
    labels = {normalized(hint)} if hint else set()
    if hostname:
        host = normalized(hostname).removeprefix("www.")
        labels.update({host, "www." + host})
    # Browser tab titles conventionally separate the site label from the page.
    # A mention such as "How to use YouTube" is deliberately insufficient.
    parts = re.split(r"\s+[-–—|]\s+", title)
    return bool(labels & {parts[0], parts[-1]})


class BrowserSelectionError(ValueError):
    def __init__(self, message, rows):
        super().__init__(message)
        self.candidates = [{key: row.get(key, "")
                            for key in ("name", "role", "automation_id", "enabled")}
                           for row in rows[:30]]


class BrowserSession:
    """Recent uses of exact tab identities, scoped to observed HWND/PID pairs."""

    def __init__(self, *, max_windows=8, max_tabs=128, ttl_seconds=300, clock=time.monotonic):
        if max_windows < 1 or max_tabs < 1 or ttl_seconds <= 0:
            raise ValueError("Browser memory bounds must be positive")
        self.max_windows, self.max_tabs = max_windows, max_tabs
        self.ttl_seconds, self.clock = ttl_seconds, clock
        self._windows = OrderedDict()
        self._captions = {}
        self._lock = threading.RLock()
        self._serial = 0

    @staticmethod
    def _key(window):
        return window.hwnd, window.pid, normalized(getattr(window, "exe", ""))

    def clear(self):
        with self._lock:
            self._windows.clear()
            self._captions.clear()
            self._serial = 0

    def forget(self, window):
        with self._lock:
            self._windows.pop(self._key(window), None)
            self._captions.pop(self._key(window), None)

    def _expire(self, window, now):
        for key, (_, updated) in list(self._windows.items()):
            if now - updated > self.ttl_seconds or (key[0] == window.hwnd and key != self._key(window)):
                del self._windows[key]
                self._captions.pop(key, None)
        for captions in self._captions.values():
            for title, (_, used) in list(captions.items()):
                if now - used > self.ttl_seconds:
                    del captions[title]

    def _touch(self, key, inventory, now):
        self._windows[key] = inventory, now
        self._windows.move_to_end(key)
        while len(self._windows) > self.max_windows:
            removed, _ = self._windows.popitem(last=False)
            self._captions.pop(removed, None)

    def observe_caption(self, window):
        """Record a verified foreground caption without UIA or a tab identity.

        A later complete tab inventory may use this recency only when the
        observed title matches one unique tab in the same window identity.
        """
        with self._lock:
            now, key = self.clock(), self._key(window)
            self._expire(window, now)
            title = normalized(page_title(window.title))
            captions = self._captions.setdefault(key, OrderedDict())
            if title and len(title) <= 1024:
                self._serial += 1
                captions[title] = self._serial, now
                captions.move_to_end(title)
                while len(captions) > self.max_tabs:
                    captions.popitem(last=False)
            self._touch(key, self._windows.get(key, ({}, now))[0], now)

    def observe(self, window, rows):
        """Refresh a complete tab inventory and remember a unique active title."""
        with self._lock:
            now = self.clock()
            self.observe_caption(window)
            key = self._key(window)
            identities = [tab_identity(row) for row in rows]
            names = [identity[0] for identity in identities]
            captions = self._captions[key]
            # Complete inventories invalidate captions that disappeared or now
            # name multiple tabs. A title alone never supplies tab identity.
            for title in list(captions):
                if names.count(title) != 1:
                    del captions[title]
            current = {identity: captions.get(identity[0], (0, now))[0] for identity in identities
                       if identity[0] and len(identity[0]) <= 1024 and identities.count(identity) == 1}
            if len(current) > self.max_tabs:
                current = dict(sorted(current.items(), key=lambda item: item[1], reverse=True)[:self.max_tabs])
            self._touch(key, current, now)

    def choose(self, window, rows, *, hint, hostname):
        """Choose one exact site match, or the uniquely most recently observed use."""
        with self._lock:
            self.observe(window, rows)
            matches = [row for row in rows if site_tab_matches(row.get("name", ""), hint, hostname)]
            if not matches:
                raise BrowserSelectionError("No matching tab was observed in this browser window.", rows)
            identities = [tab_identity(row) for row in rows]
            names = [normalized(row.get("name", "")) for row in rows]
            if any(identities.count(tab_identity(row)) != 1
                   or names.count(normalized(row.get("name", ""))) != 1 for row in matches):
                raise BrowserSelectionError("Several tabs have the same identity. Which tab should close?", matches)
            if len(matches) == 1:
                return dict(matches[0])
            known = self._windows[self._key(window)][0]
            ranks = [known.get(tab_identity(row), 0) for row in matches]
            best = max(ranks)
            if not best or ranks.count(best) != 1:
                raise BrowserSelectionError("Several matching tabs are open. Which tab should close?", matches)
            return dict(matches[ranks.index(best)])
