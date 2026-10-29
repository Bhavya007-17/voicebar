"""Shared Qt lifetime for desktop bar tests."""

import pytest


@pytest.fixture(scope="session")
def qapp():
    """Initialize Qt before per-test native COM owners and retain it to exit."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    previous = app.quitOnLastWindowClosed()
    app.setQuitOnLastWindowClosed(False)
    yield app
    app.setQuitOnLastWindowClosed(previous)
