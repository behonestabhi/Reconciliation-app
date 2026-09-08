import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models import Base

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sample")


def sample_path(name: str) -> str:
    return os.path.join(SAMPLE_DIR, name)


def read_sample_bytes(name: str) -> bytes:
    with open(sample_path(name), "rb") as f:
        return f.read()


@pytest.fixture()
def db_session():
    """A fresh, isolated in-memory SQLite database for a single test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
