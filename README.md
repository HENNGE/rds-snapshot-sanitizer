# rds-snapshot-sanitizer

Create sanitized copy of RDS snapshots and share them with selected accounts.

It works by restoring an unsanitized snapshot to a temporary cluster and executing sanitizing SQL queries against it, after which sanitized snapshot will be created and optionally shared with other accounts.

# Environment variable
- `SANITIZER_RDS_CLUSTER_ID`: RDS cluster identifier whose snapshots will be sanitized.
- `SANITIZER_CONFIG`: rds-snapshot-sanitizer configuration in JSON. See [Configuration](#configuration).
- `SANTITIZER_RDS_INSTANCE_ACU`: (Optional) ACU to be allocatted for the temporary RDS instance. Defaults to 2 ACU.
- `SANITIZER_SQL_MAX_CONNECTIONS`: (Optional) Number of maximum connections to be created for executing the SQL queries. Defaults to 20.
- `SANITIZER_SHARE_KMS_KEY_ID`: (Optional) KMS key identifier to be used for the sanitized snapshot.
- `SANITIZER_SHARE_ACCOUNT_IDS`: (Optional) List of AWS account ids to share the sanitized snapshot with.
- `SANITIZER_AWS_REGION`: (Optional) AWS region where the RDS cluster is hosted. Defaults to `AWS_REGION` or `AWS_DEFAULT_REGION` environment variable.
- `SANITIZER_DELETE_OLD_SNAPSHOTS`: (Optional) Whether to delete old snapshots. Defaults to False.
- `SANITIZER_OLD_SNAPSHOTS_DAYS`: (Optional) Number of days for a snapshot to be considered old. Defaults to 30.

# Configuration
The configuration is a JSON file with the following schema:
- `"tables"`: list of table configuration
  - `"name"`: name of the table
  - `"columns"`: list of column configuration
    - `"name"`: name of the column
    - `"sanitizer"`: type of sanitizer to be used. There are two types provided, static and random.
      - `"type"`: `"static"`
      - `"value"`: a static string value to be used for replacement.

      OR

      - `"type"`: `"random"`
      - `"kind"`: `"name"`, `"first_name"`, `"last_name"`, `"user_name"`, `"email"`, `"phone_number"`, etc. See the full list of [random providers](https://faker.readthedocs.io/en/master/providers.html).
  - `"drop_constraints"`: list of table constraints to be dropped
- `"drop_indexes"`: list of index to be dropped

Example:
```json
{
    "drop_indexes": ["users_tenant_id_email_key"],
    "tables": [
        {
            "name": "users",
            "columns": [
                {
                    "name": "family_name",
                    "sanitizer": {"type": "random", "kind": "last_name"},
                },
                {
                    "name": "given_name",
                    "sanitizer": {"type": "random", "kind": "first_name"},
                },
                {"name": "email", "sanitizer": {"type": "random", "kind": "email"}}
            ]
        },
        {
            "name": "imported_users",
            "drop_constraints": ["imported_users_tenant_id_email_key"],
            "columns": [
                {
                    "name": "family_name",
                    "sanitizer": {"type": "static", "kind": "doe"},
                },
                {
                    "name": "given_name",
                    "sanitizer": {"type": "static", "value": "john"},
                },
                {"name": "email", "sanitizer": {"type": "random", "kind": "email"}}
            ]
        }
    ]
}
```

# Running locally
The tool is meant to be run inside the same network as the RDS subnet that contains the target cluster.

To run the tool locally (for debugging, etc), you need to:
- Ensure that the temporary RDS cluster is accessible from localhost. See [port-forwarding with session manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html#sessions-remote-port-forwarding).
- Specify `--local` flag to set the RDS host target to localhost.

```bash
poetry install
poetry run sanitizer --flag
```
