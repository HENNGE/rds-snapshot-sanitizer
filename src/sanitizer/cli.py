import asyncio
from functools import wraps

import click

from .rds import (
    cleanup,
    create_instance,
    create_snapshot,
    delete_old_snapshots,
    get_latest_snapshot,
    restore_snapshot,
    rotate_password,
    share_snapshot,
)
from .settings import settings
from .sql import sanitize


def async_command(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return wrapper


@click.command()
@click.option("--local", is_flag=True, default=False, help="Run locally")
@async_command
async def main(local: bool):
    click.echo("#################### Finding latest snapshot ####################")
    snapshot = get_latest_snapshot(settings.rds_cluster_id)
    click.echo(f"Latest snapshot: {snapshot}")
    click.echo("")

    click.echo("#################### Restoring snapshot ####################")
    temp_cluster = restore_snapshot(snapshot)
    click.echo(f"Temporary cluster: {temp_cluster}")
    click.echo("")

    click.echo("#################### Rotating password ####################")
    ssm_param, temp_cluster = rotate_password(temp_cluster)
    click.echo(f"SSM parameter name: {ssm_param}")
    click.echo("")

    click.echo("#################### Creating instance ####################")
    temp_instance = create_instance(temp_cluster)
    click.echo(f"Temporary instance: {temp_instance}")
    click.echo("")

    click.echo("#################### Sanitizing ####################")
    await sanitize(temp_cluster, ssm_param, local)
    click.echo("")

    click.echo("#################### Creating sanitized snapshot ####################")
    sanitized_snapshot = create_snapshot(temp_cluster)
    click.echo(f"Sanitized snapshot: {sanitized_snapshot}")
    click.echo("")

    click.echo("#################### Creating shared snapshot ####################")
    shared_snapshot = share_snapshot(sanitized_snapshot)
    click.echo(f"Shared snapshot: {shared_snapshot}")
    click.echo("")

    click.echo("#################### Cleaning up resources ####################")
    cleanup(sanitized_snapshot, temp_instance, temp_cluster, ssm_param)
    click.echo("")

    if settings.delete_old_snapshots:
        click.echo("#################### Deleting old snapshots ####################")
        delete_old_snapshots()
        click.echo("")
