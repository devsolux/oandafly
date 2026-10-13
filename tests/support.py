"""Integration prerequisites, never a substitute database implementation."""
import sqlite3
import pytest
from oandafly.storage.database import Database

def supported_database(*args, **kwargs):
    if sqlite3.sqlite_version_info < (3, 51, 3):
        pytest.skip("Production SQLite integration requires SQLite >=3.51.3; runtime guard is not bypassed")
    return Database(*args, **kwargs)
