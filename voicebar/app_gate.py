"""Stage 7 foreground scope around the byte-preserved reference Gate."""

from dataclasses import replace

from .gate import Gate


class Stage7Gate(Gate):
    """Supply pure parser context without changing the reference gate policy."""

    def hear(self, text, now, *, active_app=None):
        catalog, open_catalog = self.catalog, self.open_catalog
        self.catalog = replace(catalog, active_app=active_app)
        self.open_catalog = replace(open_catalog, active_app=active_app)
        try:
            return super().hear(text, now)
        finally:
            self.catalog, self.open_catalog = catalog, open_catalog

    def _accept(self, plan, now):
        decision = super()._accept(plan, now)
        # App actions bring their app forward. Retain the last reference in
        # plan order, including a later open/switch/window reference handled by
        # the base gate, so a preceding app action cannot overwrite it.
        for action in plan:
            if action["action"] in {"open_app", "switch_app", "app_action"}:
                if action["app"] != "active":
                    self.last_app = action["app"]
            elif action["action"] == "window" and action["target"] != "active":
                self.last_app = action["target"]
        return decision
