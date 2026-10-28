import re

import psycopg2
from psycopg2 import sql
from psycopg2.errors import DuplicateDatabase


ALLOWED_POSTGRES_TYPES = {
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "REAL",
    "DOUBLE PRECISION",
    "BOOLEAN",
    "TEXT",
    "DATE",
    "TIMESTAMP",
    "TIMESTAMPTZ",
    "JSONB",
    "UUID",
    "NUMERIC",
    "NUMERIC(10,2)",
    "NUMERIC(12,2)",
    "NUMERIC(14,2)",
    "VARCHAR(50)",
    "VARCHAR(100)",
    "VARCHAR(255)",
}

TYPE_ALIASES = {
    "INT": "INTEGER",
    "INT4": "INTEGER",
    "INT8": "BIGINT",
    "FLOAT": "DOUBLE PRECISION",
    "BOOL": "BOOLEAN",
    "VARCHAR": "TEXT",
}


def normalize_postgres_type(value):
    normalized = TYPE_ALIASES.get(str(value).upper().strip(), str(value).upper().strip())
    if normalized not in ALLOWED_POSTGRES_TYPES:
        raise ValueError(f"Unapproved PostgreSQL type: {normalized}")
    return normalized


def _validate_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_$]{0,62}",
        value,
    ):
        raise ValueError(f"Invalid PostgreSQL identifier: {value!r}")
    return value


def _table_columns(table):
    columns = table.get("columns")
    if not isinstance(columns, list):
        raise ValueError(f"Invalid columns for table {table.get('table_name')!r}")
    validated = []
    seen = set()
    for column in columns:
        name = _validate_identifier(column.get("column_name"))
        if name in seen:
            raise ValueError(f"Duplicate column {name!r}")
        seen.add(name)
        postgres_type = normalize_postgres_type(column.get("postgres_type", "TEXT"))
        nullable = column.get("nullable", True)
        if not isinstance(nullable, bool):
            raise ValueError(f"Invalid nullable value for column {name!r}")
        validated.append(
            {
                "name": name,
                "type": postgres_type,
                "nullable": nullable,
                "primary_key": column.get("primary_key", False) is True,
            }
        )
    return validated


def _actual_type(data_type, udt_name):
    type_name = data_type.upper()
    if type_name == "USER-DEFINED":
        return udt_name.upper()
    if type_name == "CHARACTER VARYING":
        return "VARCHAR"
    if type_name == "TIMESTAMP WITHOUT TIME ZONE":
        return "TIMESTAMP"
    if type_name == "TIMESTAMP WITH TIME ZONE":
        return "TIMESTAMPTZ"
    if type_name == "DOUBLE PRECISION":
        return "DOUBLE PRECISION"
    if type_name == "ARRAY":
        return f"{udt_name.upper()}[]"
    return type_name


def ensure_database_and_schema(
    *,
    host,
    port,
    username,
    password,
    database_name,
    database_design,
):
    _validate_identifier(database_name)
    tables = database_design.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValueError("Database design must contain a non-empty tables list.")

    definitions = []
    seen_tables = set()
    for table in tables:
        table_name = _validate_identifier(table.get("table_name"))
        if table_name in seen_tables:
            raise ValueError(f"Duplicate table {table_name!r}")
        seen_tables.add(table_name)
        columns = _table_columns(table)
        if not columns:
            raise ValueError(f"Table {table_name!r} has no columns.")
        definitions.append((table_name, columns))

    connection_args = {
        "host": host,
        "port": port,
        "user": username,
        "password": password,
        "connect_timeout": 10,
    }
    result = {
        "database_name": database_name,
        "database_created": False,
        "tables_created": [],
        "columns_added": [],
        "type_conflicts": [],
        "nullability_conflicts": [],
        "primary_key_conflicts": [],
        "nullability_notes": [],
    }

    with psycopg2.connect(dbname="postgres", **connection_args) as admin_connection:
        admin_connection.autocommit = True
        with admin_connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (database_name,),
            )
            exists = cursor.fetchone() is not None
            if not exists:
                try:
                    cursor.execute(
                        sql.SQL("CREATE DATABASE {}").format(
                            sql.Identifier(database_name)
                        )
                    )
                    result["database_created"] = True
                except DuplicateDatabase:
                    exists = True

    with psycopg2.connect(dbname=database_name, **connection_args) as target_connection:
        with target_connection:
            with target_connection.cursor() as cursor:
                for table_name, columns in definitions:
                    cursor.execute(
                        """
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name = %s
                          AND table_type = 'BASE TABLE'
                        """,
                        (table_name,),
                    )
                    table_exists = cursor.fetchone() is not None
                    if not table_exists:
                        definitions_sql = []
                        primary_keys = []
                        for column in columns:
                            column_sql = sql.SQL("{} {}").format(
                                sql.Identifier(column["name"]),
                                sql.SQL(column["type"]),
                            )
                            if not column["nullable"]:
                                column_sql += sql.SQL(" NOT NULL")
                            definitions_sql.append(column_sql)
                            if column["primary_key"]:
                                primary_keys.append(column["name"])
                        if primary_keys:
                            definitions_sql.append(
                                sql.SQL("PRIMARY KEY ({})").format(
                                    sql.SQL(", ").join(
                                        sql.Identifier(name) for name in primary_keys
                                    )
                                )
                            )
                        cursor.execute(
                            sql.SQL("CREATE TABLE {} ({})").format(
                                sql.Identifier("public", table_name),
                                sql.SQL(", ").join(definitions_sql),
                            )
                        )
                        result["tables_created"].append(table_name)
                        continue

                    cursor.execute(
                        """
                        SELECT
                            columns.column_name,
                            columns.data_type,
                            columns.udt_name,
                            columns.is_nullable,
                            columns.character_maximum_length,
                            columns.numeric_precision,
                            columns.numeric_scale,
                            EXISTS (
                                SELECT 1
                                FROM information_schema.table_constraints AS constraints
                                JOIN information_schema.key_column_usage AS key_columns
                                  ON key_columns.constraint_catalog = constraints.constraint_catalog
                                 AND key_columns.constraint_schema = constraints.constraint_schema
                                 AND key_columns.constraint_name = constraints.constraint_name
                                WHERE constraints.table_schema = 'public'
                                  AND constraints.table_name = columns.table_name
                                  AND constraints.constraint_type = 'PRIMARY KEY'
                                  AND key_columns.column_name = columns.column_name
                            ) AS is_primary_key
                        FROM information_schema.columns AS columns
                        WHERE columns.table_schema = 'public'
                          AND columns.table_name = %s
                        """,
                        (table_name,),
                    )
                    existing_columns = {
                        row[0]: {
                            "type": _actual_type(row[1], row[2]),
                            "nullable": row[3] == "YES",
                            "character_maximum_length": row[4],
                            "numeric_precision": row[5],
                            "numeric_scale": row[6],
                            "primary_key": row[7],
                        }
                        for row in cursor.fetchall()
                    }
                    for column in columns:
                        existing = existing_columns.get(column["name"])
                        if existing:
                            expected_type = column["type"]
                            actual_type = existing["type"]
                            if actual_type == "VARCHAR" and existing[
                                "character_maximum_length"
                            ]:
                                actual_type += (
                                    f"({existing['character_maximum_length']})"
                                )
                            if (
                                actual_type == "NUMERIC"
                                and existing["numeric_precision"] is not None
                            ):
                                actual_type = (
                                    f"NUMERIC({existing['numeric_precision']},"
                                    f"{existing['numeric_scale'] or 0})"
                                )
                            if expected_type != actual_type:
                                result["type_conflicts"].append(
                                    {
                                        "table": table_name,
                                        "column": column["name"],
                                        "expected": column["type"],
                                        "actual": actual_type,
                                    }
                                )
                            if existing["nullable"] != column["nullable"]:
                                result["nullability_conflicts"].append(
                                    {
                                        "table": table_name,
                                        "column": column["name"],
                                        "expected_nullable": column["nullable"],
                                        "actual_nullable": existing["nullable"],
                                    }
                                )
                            if existing["primary_key"] != column["primary_key"]:
                                result["primary_key_conflicts"].append(
                                    {
                                        "table": table_name,
                                        "column": column["name"],
                                        "expected_primary_key": column["primary_key"],
                                        "actual_primary_key": existing["primary_key"],
                                    }
                                )
                            continue

                        cursor.execute(
                            sql.SQL("SELECT EXISTS (SELECT 1 FROM {} LIMIT 1)").format(
                                sql.Identifier("public", table_name)
                            )
                        )
                        table_has_rows = cursor.fetchone()[0]
                        add_column = sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(
                            sql.Identifier("public", table_name),
                            sql.Identifier(column["name"]),
                            sql.SQL(column["type"]),
                        )
                        if not column["nullable"] and not table_has_rows:
                            add_column += sql.SQL(" NOT NULL")
                        elif not column["nullable"]:
                            result["nullability_notes"].append(
                                {
                                    "table": table_name,
                                    "column": column["name"],
                                    "note": (
                                        "Added nullable because the table already "
                                        "contains rows; no backfill value was provided."
                                    ),
                                }
                            )
                        cursor.execute(add_column)
                        result["columns_added"].append(
                            {"table": table_name, "column": column["name"]}
                        )

    result["database_exists"] = not result["database_created"]
    return result
