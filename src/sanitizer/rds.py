import itertools
import os
import secrets
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

    # Wait until new cluster is active
    wait_resource(
        "db_cluster_available",
        {"DBClusterIdentifier": restored_cluster["DBClusterIdentifier"]},
        "Restoring to temporary cluster",
        "Timed out when restoring cluster",
    )

    # Disable AutoMinorVersionUpgrade, set PreferredBackupWindow and BackupRetentionPeriod
    restored_cluster = rds_client.modify_db_cluster(
        DBClusterIdentifier=restored_cluster["DBClusterIdentifier"],
        AutoMinorVersionUpgrade=False,
        BackupRetentionPeriod=1,
        PreferredBackupWindow="22:00-22:30",
        BackupRetentionPeriod=1,
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
