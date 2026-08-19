import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.database import get_session
from app.main import app


@pytest.fixture
def client():
    async def override_get_session():
        yield AsyncMock()

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
