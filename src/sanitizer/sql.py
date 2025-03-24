import asyncio

import click
from psycopg import AsyncClientCursor, sql
from psycopg_pool import AsyncConnectionPool
from types_boto3_rds.type_defs import DBClusterTypeDef

from .faker import fake
from .rds import get_password
from .settings import Table, settings


async def drop_index(index: str, pool: AsyncConnectionPool):
    query = sql.SQL("DROP INDEX IF EXISTS {index}").format(
        index=sql.Identifier(index),
    )
    click.echo(query.as_string())
    async with pool.connection() as aconn:
        await aconn.execute(query)


async def drop_table_constraint(
    table_name: str, constraint: str, pool: AsyncConnectionPool
):
    query = sql.SQL(
        "ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS {constraint}"
    ).format(
        table_name=sql.Identifier(table_name),
        constraint=sql.Identifier(constraint),
    )
    click.echo(query.as_string())
    async with pool.connection() as aconn:
        await aconn.execute(query)


async def sanitize_table(table: Table, pool: AsyncConnectionPool) -> int:
    query = sql.SQL("UPDATE {table_name} SET ({columns}) = ROW ({values})").format(
        table_name=sql.Identifier(table.name),
        columns=sql.SQL(", ").join(
            map(
                lambda col: sql.Identifier(col.name),
                table.columns,
            )
        ),
        values=sql.SQL(", ").join(sql.Placeholder() * len(table.columns)),
    )
    click.echo(query.as_string())
    async with pool.connection() as aconn:
        return (
            await aconn.execute(
                query,
                [
                    col.sanitizer.value
                    if col.sanitizer.type == "static"
                    else getattr(fake, col.sanitizer.kind)()
                    for col in table.columns
                ],
            )
        ).rowcount


async def sanitize(cluster: DBClusterTypeDef, ssm_param: str) -> None:
    cluster["Endpoint"] = "localhost"
    conn_string = (
        f"postgresql://{cluster['MasterUsername']}:{get_password(ssm_param)}"
        f"@{cluster['Endpoint']}:{cluster['Port']}"
        f"/{cluster['DatabaseName']}?sslmode=require"
    )
    async with AsyncConnectionPool(
        conn_string,
        max_size=settings.sql_max_connections,
        kwargs={"cursor_factory": AsyncClientCursor},
    ) as pool:
        await asyncio.gather(
            *[drop_index(index, pool) for index in settings.config.drop_indexes]
        )
        await asyncio.gather(
            *[
                drop_table_constraint(table.name, constraint, pool)
                for table in settings.config.tables
                for constraint in table.drop_constraints
            ]
        )
        rowcounts = await asyncio.gather(
            *[sanitize_table(table, pool) for table in settings.config.tables]
        )
        for i, table in enumerate(settings.config.tables):
            print(f"{rowcounts[i]} rows sanitized in table '{table.name}'")
