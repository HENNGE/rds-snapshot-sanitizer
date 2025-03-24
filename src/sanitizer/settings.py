from typing import Any, Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from sanitizer.faker import FakerEnum


class Static(BaseModel):
    type: Literal["static"]
    value: Any


class Random(BaseModel):
    type: Literal["random"]
    kind: FakerEnum


class Column(BaseModel):
    name: str
    sanitizer: Static | Random


class Table(BaseModel):
    name: str
    columns: list[Column]
    drop_constraints: list[str] = []


class Config(BaseModel):
    tables: list[Table] = []
    drop_indexes: list[str] = []


class Settings(BaseSettings):
    rds_cluster_id: str
    rds_instance_acu: int = 2
    sql_max_connections: int = 20
    share_kms_key_id: str | None = None
    share_account_ids: list[str] = []
    aws_region: str | None = None
    delete_old_snapshots: bool = False
    old_snapshots_days: int = 30
    config: Config = Config()

    model_config = SettingsConfigDict(env_prefix="sanitizer_")


settings = Settings()
