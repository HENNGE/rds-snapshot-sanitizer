import itertools
import os
import secrets
import time
from datetime import UTC, datetime

import boto3
import click
from botocore.exceptions import WaiterError
from types_boto3_rds.type_defs import (
    DBClusterSnapshotTypeDef,
    DBClusterTypeDef,
    DBInstanceTypeDef,
    ServerlessV2ScalingConfigurationTypeDef,
    WaiterConfigTypeDef,
)

from .settings import settings

rds_client = boto3.client("rds")
ssm_client = boto3.client("ssm")

# Used unless the cluster is still "creating", and if the progress check breaks.
_FALLBACK_RESTORE_MINS = 180
_MAX_RESTORE_MINS = 12 * 60
_POLL_SECS = 60
_TERMINAL_CLUSTER_STATUSES = frozenset(
    {"deleted", "deleting", "failed", "incompatible-restore", "incompatible-parameters"}
)


def wait_resource(
    waiter: str,
    resource: dict[str, str],
    start_msg: str,
    timeout_msg: str,
    wait_mins: int = 60,
):
    click.echo(start_msg, nl=False)
    waiter = rds_client.get_waiter(waiter)
    finished = False
    for _ in range(wait_mins):
        try:
            waiter.wait(
                WaiterConfig=WaiterConfigTypeDef(Delay=60, MaxAttempts=2), **resource
            )
            finished = True
            break
        except WaiterError:
            click.echo(".", nl=False)
    click.echo("")
    if not finished:
        raise TimeoutError(timeout_msg)


def _cluster_status(response: dict | None) -> str | None:
    try:
        clusters = (response or {}).get("DBClusters") or []
        status = clusters[0].get("Status") if clusters else None
        return status if isinstance(status, str) and status else None
    except Exception:
        return None


def _log_cluster_events(
    cluster_id: str, started_at: datetime, seen: set[tuple[str, str]]
) -> None:
    response = rds_client.describe_events(
        SourceIdentifier=cluster_id,
        SourceType="db-cluster",
        StartTime=started_at,
        EndTime=datetime.now(UTC),
        MaxRecords=100,
    )
    for event in response.get("Events") or []:
        when = event.get("Date")
        message = event.get("Message") or ""
        if (key := (str(when), message)) not in seen:
            seen.add(key)
            click.echo(f"RDS event {when}: {message}")


def wait_for_cluster_restore(cluster_id: str) -> None:
    """Wait for restore. Only a ``creating`` status may use the longer cap."""
    try:
        _wait_for_cluster_restore(cluster_id)
    except TimeoutError:
        raise
    except Exception as exc:
        click.echo(
            f"Restore progress check failed ({type(exc).__name__}: {exc}). "
            f"Falling back to the {_FALLBACK_RESTORE_MINS} minute waiter."
        )
        wait_resource(
            "db_cluster_available",
            {"DBClusterIdentifier": cluster_id},
            "Restoring to temporary cluster",
            "Timed out when restoring cluster",
            _FALLBACK_RESTORE_MINS,
        )


def _wait_for_cluster_restore(cluster_id: str) -> None:
    cap_mins = min(
        max(int(settings.restore_timeout_mins), _FALLBACK_RESTORE_MINS),
        _MAX_RESTORE_MINS,
    )
    start = time.monotonic()
    started_at = datetime.now(UTC)
    seen_events: set[tuple[str, str]] = set()
    log_events = True
    last_status: str | None = None
    click.echo(
        "Restoring to temporary cluster "
        f"(up to {cap_mins} min while creating, otherwise {_FALLBACK_RESTORE_MINS} min)"
    )
    waiter = rds_client.get_waiter("db_cluster_available")

    while True:
        poll_started = time.monotonic()
        try:
            waiter.wait(
                WaiterConfig=WaiterConfigTypeDef(Delay=_POLL_SECS, MaxAttempts=2),
                DBClusterIdentifier=cluster_id,
            )
            return
        except WaiterError as exc:
            last_status = _cluster_status(getattr(exc, "last_response", None))

        if log_events:
            try:
                _log_cluster_events(cluster_id, started_at, seen_events)
            except Exception as exc:
                log_events = False
                click.echo(f"RDS event logging disabled after an error: {exc}")

        if last_status in _TERMINAL_CLUSTER_STATUSES:
            raise TimeoutError(
                f"Timed out when restoring cluster (terminal status: {last_status})"
            )

        elapsed = time.monotonic() - start
        limit_mins = cap_mins if last_status == "creating" else _FALLBACK_RESTORE_MINS
        click.echo(
            f"Cluster status: {last_status} elapsed={int(elapsed)}s "
            f"remaining={max(int(limit_mins * 60 - elapsed), 0)}s"
        )
        if elapsed >= limit_mins * 60:
            raise TimeoutError(
                "Timed out when restoring cluster "
                f"(last observed status: {last_status})"
            )
        # The waiter sleeps between its own attempts. If it returned immediately,
        # pause so an unexpected status cannot busy-loop.
        if (spent := time.monotonic() - poll_started) < _POLL_SECS:
            time.sleep(_POLL_SECS - spent)


def get_latest_snapshot(rds_cluster_id: str) -> DBClusterSnapshotTypeDef:
    page_iterator = rds_client.get_paginator("describe_db_cluster_snapshots").paginate(
        DBClusterIdentifier=rds_cluster_id, SnapshotType="automated"
    )
    return sorted(
        itertools.chain.from_iterable(
            map(lambda page: page["DBClusterSnapshots"], page_iterator)
        ),
        key=lambda snapshot: snapshot["SnapshotCreateTime"],
    )[-1]


def restore_snapshot(snapshot: DBClusterSnapshotTypeDef) -> DBClusterTypeDef:
    # Describe original cluster
    cluster = rds_client.describe_db_clusters(
        DBClusterIdentifier=snapshot["DBClusterIdentifier"]
    )["DBClusters"][0]

    # Restore snapshot to new cluster
    restored_cluster = rds_client.restore_db_cluster_from_snapshot(
        AvailabilityZones=snapshot["AvailabilityZones"],
        DBClusterIdentifier=snapshot["DBClusterSnapshotIdentifier"].removeprefix(
            "rds:"
        ),
        SnapshotIdentifier=snapshot["DBClusterSnapshotIdentifier"],
        Engine=snapshot["Engine"],
        EngineVersion=snapshot["EngineVersion"],
        DBSubnetGroupName=cluster["DBSubnetGroup"],
        DatabaseName=cluster["DatabaseName"],
        VpcSecurityGroupIds=[
            sg["VpcSecurityGroupId"] for sg in cluster["VpcSecurityGroups"]
        ],
        Tags=snapshot["TagList"],
        EngineMode="provisioned",
        DBClusterParameterGroupName=cluster["DBClusterParameterGroup"],
        CopyTagsToSnapshot=True,
        PubliclyAccessible=False,
        ServerlessV2ScalingConfiguration=ServerlessV2ScalingConfigurationTypeDef(
            MinCapacity=0.5, MaxCapacity=settings.rds_instance_acu
        ),
    )["DBCluster"]

    click.echo(
        "Restoring "
        f"{restored_cluster['DBClusterIdentifier']} from snapshot "
        f"{snapshot['DBClusterSnapshotIdentifier']} "
        f"created at {snapshot['SnapshotCreateTime']}"
    )
    wait_for_cluster_restore(restored_cluster["DBClusterIdentifier"])

    # Disable AutoMinorVersionUpgrade and set PreferredBackupWindow
    restored_cluster = rds_client.modify_db_cluster(
        DBClusterIdentifier=restored_cluster["DBClusterIdentifier"],
        AutoMinorVersionUpgrade=False,
        BackupRetentionPeriod=1,
        PreferredBackupWindow="22:00-22:30",
        ApplyImmediately=True,
    )["DBCluster"]

    return restored_cluster


def rotate_password(cluster: DBClusterTypeDef) -> tuple[str, DBClusterTypeDef]:
    # Create a new password
    password = secrets.token_urlsafe(10)

    # Store password in SSM
    parameter_name = f"/RDS/{cluster['DBClusterIdentifier']}/password"
    ssm_client.put_parameter(
        Name=parameter_name,
        Value=password,
        Type="SecureString",
        Description=f"Password for {cluster['DBClusterIdentifier']} RDS cluster",
        Overwrite=True,
    )

    # Wait until cluster is available
    wait_resource(
        "db_cluster_available",
        {"DBClusterIdentifier": cluster["DBClusterIdentifier"]},
        "Waiting until cluster is available",
        "Timed out waiting for cluster to become available",
    )

    # Rotate cluster password
    cluster = rds_client.modify_db_cluster(
        DBClusterIdentifier=cluster["DBClusterIdentifier"],
        MasterUserPassword=password,
        ApplyImmediately=True,
    )["DBCluster"]

    return parameter_name, cluster


def create_instance(cluster: DBClusterTypeDef) -> DBInstanceTypeDef:
    temp_instance = rds_client.create_db_instance(
        DBClusterIdentifier=cluster["DBClusterIdentifier"],
        DBInstanceIdentifier=f"{cluster['DBClusterIdentifier']}-inst",
        DBInstanceClass="db.serverless",
        Engine=cluster["Engine"],
        DBSubnetGroupName=cluster["DBSubnetGroup"],
        BackupRetentionPeriod=0,
        AutoMinorVersionUpgrade=False,
    )["DBInstance"]

    # Wait until new instance is active
    wait_resource(
        "db_instance_available",
        {"DBInstanceIdentifier": temp_instance["DBInstanceIdentifier"]},
        "Creating instance",
        "Timed out when creating instance",
    )

    temp_instance = rds_client.describe_db_instances(
        DBInstanceIdentifier=temp_instance["DBInstanceIdentifier"],
    )["DBInstances"][0]

    return temp_instance


def get_password(ssm_param: str) -> str:
    return ssm_client.get_parameter(Name=ssm_param, WithDecryption=True)["Parameter"][
        "Value"
    ]


def create_snapshot(cluster: DBClusterTypeDef) -> DBClusterSnapshotTypeDef:
    # Create snapshot
    snapshot = rds_client.create_db_cluster_snapshot(
        DBClusterIdentifier=cluster["DBClusterIdentifier"],
        DBClusterSnapshotIdentifier=f"{cluster['DBClusterIdentifier']}-sanitized",
    )["DBClusterSnapshot"]

    # Wait until snapshot is available
    wait_resource(
        "db_cluster_snapshot_available",
        {"DBClusterSnapshotIdentifier": snapshot["DBClusterSnapshotIdentifier"]},
        "Creating snapshot",
        "Timed out wating for snapshot to become available",
    )

    return snapshot


def share_snapshot(snapshot: DBClusterSnapshotTypeDef) -> DBClusterSnapshotTypeDef:
    # Copy snapshot
    region = (
        settings.aws_region
        if settings.aws_region is not None
        else os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION"))
    )
    copied_snapshot = rds_client.copy_db_cluster_snapshot(
        SourceDBClusterSnapshotIdentifier=snapshot["DBClusterSnapshotIdentifier"],
        TargetDBClusterSnapshotIdentifier=f"{snapshot['DBClusterIdentifier']}-shared",
        CopyTags=True,
        **(
            (
                {"KmsKeyId": settings.share_kms_key_id}
                if settings.share_kms_key_id is not None
                else {}
            )
            | ({"SourceRegion": region} if region is not None else {})
        ),
    )["DBClusterSnapshot"]

    # Wait until snapshot is available
    wait_resource(
        "db_cluster_snapshot_available",
        {"DBClusterSnapshotIdentifier": copied_snapshot["DBClusterSnapshotIdentifier"]},
        "Copying snapshot",
        "Timed out when copying snapshot",
    )

    # Share snapshot
    if settings.share_account_ids:
        rds_client.modify_db_cluster_snapshot_attribute(
            DBClusterSnapshotIdentifier=copied_snapshot["DBClusterSnapshotIdentifier"],
            AttributeName="restore",
            ValuesToAdd=settings.share_account_ids,
        )

    return copied_snapshot


def cleanup(
    sanitized_snapshot: DBClusterSnapshotTypeDef,
    temp_instance: DBInstanceTypeDef,
    temp_cluster: DBClusterTypeDef,
    ssm_param: str,
):
    # Delete sanitized snapshot
    deleted_snapshot = rds_client.delete_db_cluster_snapshot(
        DBClusterSnapshotIdentifier=sanitized_snapshot["DBClusterSnapshotIdentifier"]
    )["DBClusterSnapshot"]
    wait_resource(
        "db_cluster_snapshot_deleted",
        {
            "DBClusterSnapshotIdentifier": deleted_snapshot[
                "DBClusterSnapshotIdentifier"
            ]
        },
        "Deleting sanitized snapshot",
        "Timed out when deleting sanitized snapshot",
    )
    click.echo(f"Deleted snapshot '{deleted_snapshot['DBClusterSnapshotIdentifier']}'")

    # Delete temp instance
    deleted_instance = rds_client.delete_db_instance(
        DBInstanceIdentifier=temp_instance["DBInstanceIdentifier"],
        SkipFinalSnapshot=True,
        DeleteAutomatedBackups=True,
    )["DBInstance"]
    wait_resource(
        "db_instance_deleted",
        {"DBInstanceIdentifier": deleted_instance["DBInstanceIdentifier"]},
        "Deleting temporary instance",
        "Timed out when deleting temporary instance",
    )
    click.echo(f"Deleted instance '{deleted_instance['DBInstanceIdentifier']}'")

    # Delete temp cluster
    deleted_cluster = rds_client.delete_db_cluster(
        DBClusterIdentifier=temp_cluster["DBClusterIdentifier"],
        SkipFinalSnapshot=True,
        DeleteAutomatedBackups=True,
    )["DBCluster"]
    wait_resource(
        "db_cluster_deleted",
        {"DBClusterIdentifier": deleted_cluster["DBClusterIdentifier"]},
        "Deleting temporary cluster",
        "Timed out when deleting temporary cluster",
    )
    click.echo(f"Deleted '{deleted_cluster['DBClusterIdentifier']}'")

    # Delete ssm parameter
    ssm_client.delete_parameter(Name=ssm_param)
    click.echo(f"Deleted SSM parameter '{ssm_param}'")


def delete_old_snapshots():
    def snapshot_is_old(snapshot: DBClusterSnapshotTypeDef) -> bool:
        return (
            datetime.now(UTC) - snapshot["SnapshotCreateTime"]
        ).days > settings.old_snapshots_days

    def snapshot_cluster_match(snapshot: DBClusterSnapshotTypeDef) -> bool:
        return snapshot["DBClusterIdentifier"].startswith(settings.rds_cluster_id)

    def snapshot_name_match(snapshot: DBClusterSnapshotTypeDef) -> bool:
        return snapshot["DBClusterSnapshotIdentifier"].endswith("-shared")

    page_iterator = rds_client.get_paginator("describe_db_cluster_snapshots").paginate(
        SnapshotType="manual"
    )

    old_snapshots = filter(
        lambda snapshot: snapshot_cluster_match(snapshot)
        and snapshot_name_match(snapshot)
        and snapshot_is_old(snapshot),
        itertools.chain.from_iterable(
            map(lambda page: page["DBClusterSnapshots"], page_iterator)
        ),
    )

    for snapshot in old_snapshots:
        deleted_snapshot = rds_client.delete_db_cluster_snapshot(
            DBClusterSnapshotIdentifier=snapshot["DBClusterSnapshotIdentifier"]
        )["DBClusterSnapshot"]
        try:
            wait_resource(
                "db_cluster_snapshot_deleted",
                {
                    "DBClusterSnapshotIdentifier": deleted_snapshot[
                        "DBClusterSnapshotIdentifier"
                    ]
                },
                f"Deleting snapshot '{deleted_snapshot['DBClusterSnapshotIdentifier']}'",
                f"Timed out when deleting snapshot '{deleted_snapshot['DBClusterSnapshotIdentifier']}'",
            )
            click.echo(
                f"Deleted snapshot '{deleted_snapshot['DBClusterSnapshotIdentifier']}'"
            )
        except TimeoutError as exc:
            click.echo(exc)
