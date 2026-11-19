from __future__ import annotations
from pathlib import Path

PROJECT_NAME = "Tridock"
API_PORT = 10900
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DB_PATH = DATA_DIR / "tridock.db"
