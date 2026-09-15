"""
0001_initial: Baseline migration.
Marks baseline schema established by app.db_init as initialized.
"""
from yoyo import step

__depends__ = {}

steps = [
    step(
        "SELECT 1",
        "SELECT 1"
    )
]
