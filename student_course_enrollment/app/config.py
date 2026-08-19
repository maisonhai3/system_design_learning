from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://enrollment:enrollment@localhost:5432/enrollment"
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    spool_dir: str = "./spool"
    redis_timeout_seconds: float = 1.0
    poll_interval_seconds: float = 1.0
    status_cache_ttl_seconds: int = 300

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "+psycopg2")


settings = Settings()
