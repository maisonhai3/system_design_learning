from app.config import Settings


def test_sync_database_url_swaps_driver():
    settings = Settings(database_url="postgresql+asyncpg://u:p@host:5432/db")
    assert settings.sync_database_url == "postgresql+psycopg2://u:p@host:5432/db"
