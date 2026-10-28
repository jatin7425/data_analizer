import re
from hashlib import sha256

import pandas as pd
import psycopg2
from psycopg2 import sql


def compare_schema(dataframe, expected_profile):
    uploaded_columns = [str(column).strip() for column in dataframe.columns]
    expected_columns = [
        str(column["column_name"]).strip()
        for column in expected_profile
        if isinstance(column, dict) and "column_name" in column
    ]
    uploaded_positions = {}
    for position, column in enumerate(uploaded_columns):
        uploaded_positions.setdefault(column, position)
    mismatch = {
        "missing_columns": sorted(set(expected_columns) - set(uploaded_columns)),
        "unexpected_columns": sorted(set(uploaded_columns) - set(expected_columns)),
        "type_mismatches": [],
        "duplicate_columns": sorted(
            {
                column
                for column in uploaded_columns
                if uploaded_columns.count(column) > 1
            }
            | {
                column
                for column in expected_columns
                if expected_columns.count(column) > 1
            }
        ),
    }

    profile_by_name = {
        str(column["column_name"]).strip(): column
        for column in expected_profile
        if isinstance(column, dict) and "column_name" in column
    }
    for column in set(uploaded_columns) & set(expected_columns):
        expected_type = profile_by_name[column].get("pandas_dtype")
        actual_type = str(dataframe.iloc[:, uploaded_positions[column]].dtype)
        if expected_type and actual_type != expected_type:
            mismatch["type_mismatches"].append(
                {
                    "column": column,
                    "expected": expected_type,
                    "actual": actual_type,
                }
            )
    mismatch["type_mismatches"].sort(key=lambda item: item["column"])
    return mismatch


def _column_identifier(value, context):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_$]{0,62}",
        value,
    ):
        raise ValueError(f"Invalid target identifier in mapping plan: {context}.")
    return value


def _default_upsert_key(mapped_columns):
    return list(mapped_columns)


def _prepare_tables(mapping_plan, dataframe):
    tables = mapping_plan.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValueError("The mapping plan must contain a non-empty tables list.")

    prepared = {}
    for table in tables:
        table_name = _column_identifier(table.get("table_name"), "table name")
        if table_name in prepared:
            raise ValueError(f"Duplicate table {table_name!r} in the mapping plan.")

        columns = table.get("columns")
        if not isinstance(columns, list):
            raise ValueError(f"Invalid column mapping for table {table_name!r}.")
        sources = {}
        primary_keys = []
        for column in columns:
            target_column = _column_identifier(
                column.get("target_column"),
                f"{table_name} column",
            )
            source_columns = column.get("source_columns") or []
            if column.get("include") and len(source_columns) != 1:
                raise ValueError(
                    f"{table_name}.{target_column} must map to exactly one "
                    "Excel column before it can be imported."
                )
            if column.get("include"):
                source_column = source_columns[0]
                if source_column not in dataframe.columns:
                    raise ValueError(
                        f"Mapping for {table_name}.{target_column} refers to "
                        f"missing Excel column {source_column!r}."
                    )
                sources[target_column] = source_column
            if column.get("primary_key"):
                if target_column not in sources:
                    raise ValueError(
                        f"Primary key {table_name}.{target_column} has no "
                        "source-backed Excel mapping."
                    )
                primary_keys.append(target_column)
        inferred_key = False
        if not primary_keys:
            primary_keys = _default_upsert_key(sources)
            inferred_key = bool(primary_keys)
            if not inferred_key:
                raise ValueError(
                    f"Table {table_name!r} has no source-backed primary key or "
                    "mapped columns to use as an upsert key."
                )
            key_sources = [sources[column] for column in primary_keys]
            if dataframe[key_sources].isna().any().any():
                raise ValueError(
                    f"Table {table_name!r} has blank values in the inferred "
                    f"composite upsert key {primary_keys}."
                )
        if not sources:
            raise ValueError(f"Table {table_name!r} has no mapped Excel columns.")

        prepared[table_name] = {
            "columns": sources,
            "primary_keys": primary_keys,
            "ensure_upsert_key_unique": inferred_key,
            "foreign_keys": table.get("foreign_keys") or [],
        }

    for table_name, table in prepared.items():
        for foreign_key in table["foreign_keys"]:
            strategy = foreign_key.get("strategy")
            target_column = _column_identifier(
                foreign_key.get("column"),
                f"{table_name} foreign key",
            )
            if target_column not in table["columns"]:
                raise ValueError(
                    f"Foreign key {table_name}.{target_column} is not mapped "
                    "from the Excel sheet."
                )
            if strategy == "direct":
                continue
            if strategy != "lookup":
                raise ValueError(
                    f"Foreign key {table_name}.{target_column} "
                    "needs a reviewed mapping strategy before import."
                )
            source_columns = foreign_key.get("source_columns") or []
            lookup_by = foreign_key.get("lookup_by") or []
            parent_table = foreign_key.get("references_table")
            parent_column = foreign_key.get("references_column")
            if (
                len(source_columns) != 1
                or len(lookup_by) != 1
                or parent_table not in prepared
                or not parent_column
            ):
                raise ValueError(
                    f"Foreign key mapping for {table_name}.{target_column} "
                    "is incomplete."
                )
            parent_table = _column_identifier(
                parent_table,
                f"{table_name} referenced table",
            )
            parent_column = _column_identifier(
                parent_column,
                f"{table_name} referenced column",
            )
            lookup_column = _column_identifier(
                lookup_by[0],
                f"{table_name} lookup column",
            )
            if lookup_column not in prepared[parent_table]["columns"]:
                raise ValueError(
                    f"Lookup column {parent_table}.{lookup_column} is not "
                    "mapped from the Excel sheet."
                )
            if source_columns[0] not in dataframe.columns:
                raise ValueError(
                    f"Foreign key {table_name}.{target_column} refers to "
                    f"missing Excel column {source_columns[0]!r}."
                )
            foreign_key["validated_target_column"] = target_column
            foreign_key["validated_parent_table"] = parent_table
            foreign_key["validated_parent_column"] = parent_column
            foreign_key["validated_lookup_column"] = lookup_column
            foreign_key["validated_source_column"] = source_columns[0]

    ordered_names = []
    remaining = set(prepared)
    while remaining:
        ready = [
            name
            for name in prepared
            if name in remaining
            and all(
                foreign_key.get("references_table") not in remaining
                or foreign_key.get("references_table") == name
                for foreign_key in prepared[name]["foreign_keys"]
            )
        ]
        if not ready:
            raise ValueError(
                "The mapping plan contains a circular foreign-key dependency."
            )
        ordered_names.extend(ready)
        remaining.difference_update(ready)
    return [(name, prepared[name]) for name in ordered_names]


def _database_value(value):
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if hasattr(value, "item"):
        return value.item()
    return value


def upsert_dataframe(*, connection, dataframe, mapping_plan):
    tables = _prepare_tables(mapping_plan, dataframe)
    results = []
    with connection:
        with connection.cursor() as cursor:
            for table_name, table in tables:
                target_columns = list(table["columns"])
                conflict_columns = table["primary_keys"]
                if table["ensure_upsert_key_unique"]:
                    cursor.execute(
                        sql.SQL(
                            "SELECT {} FROM {} GROUP BY {} "
                            "HAVING COUNT(*) > 1 LIMIT 1"
                        ).format(
                            sql.SQL(", ").join(
                                sql.Identifier(column)
                                for column in conflict_columns
                            ),
                            sql.Identifier("public", table_name),
                            sql.SQL(", ").join(
                                sql.Identifier(column)
                                for column in conflict_columns
                            ),
                        )
                    )
                    if cursor.fetchone() is not None:
                        raise ValueError(
                            f"Existing rows in {table_name!r} contain duplicate "
                            f"values for inferred upsert key {conflict_columns}. "
                            "Resolve those duplicates before importing."
                        )
                    key_hash = sha256(
                        f"{table_name}:{','.join(conflict_columns)}".encode("utf-8")
                    ).hexdigest()[:16]
                    index_name = (
                        f"analyzer_upsert_{key_hash}_key_uq"
                    )
                    cursor.execute(
                        sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} ({})").format(
                            sql.Identifier(index_name),
                            sql.Identifier("public", table_name),
                            sql.SQL(", ").join(
                                sql.Identifier(column)
                                for column in conflict_columns
                            ),
                        )
                    )
                updates = [
                    column for column in target_columns
                    if column not in conflict_columns
                ]
                if updates:
                    conflict_action = sql.SQL("DO UPDATE SET {}").format(
                        sql.SQL(", ").join(
                            sql.SQL("{} = EXCLUDED.{}").format(
                                sql.Identifier(column),
                                sql.Identifier(column),
                            )
                            for column in updates
                        )
                    )
                else:
                    conflict_action = sql.SQL("DO NOTHING")
                statement = sql.SQL(
                    "INSERT INTO {} ({}) VALUES ({}) "
                    "ON CONFLICT ({}) {}"
                ).format(
                    sql.Identifier("public", table_name),
                    sql.SQL(", ").join(
                        sql.Identifier(column) for column in target_columns
                    ),
                    sql.SQL(", ").join(sql.Placeholder() for _ in target_columns),
                    sql.SQL(", ").join(
                        sql.Identifier(column) for column in conflict_columns
                    ),
                    conflict_action,
                )
                processed = 0
                for source_row in dataframe.to_dict(orient="records"):
                    values = {
                        target_column: _database_value(source_row[source_column])
                        for target_column, source_column in table["columns"].items()
                    }
                    for foreign_key in table["foreign_keys"]:
                        if foreign_key.get("strategy") == "direct":
                            continue
                        source_value = _database_value(
                            source_row[foreign_key["validated_source_column"]]
                        )
                        if source_value is None:
                            values[foreign_key["validated_target_column"]] = None
                            continue
                        cursor.execute(
                            sql.SQL("SELECT {} FROM {} WHERE {} = %s LIMIT 2").format(
                                sql.Identifier(
                                    foreign_key["validated_parent_column"]
                                ),
                                sql.Identifier(
                                    "public",
                                    foreign_key["validated_parent_table"],
                                ),
                                sql.Identifier(
                                    foreign_key["validated_lookup_column"]
                                ),
                            ),
                            (source_value,),
                        )
                        matches = cursor.fetchall()
                        if len(matches) != 1:
                            raise ValueError(
                                f"Could not uniquely resolve {table_name}."
                                f"{foreign_key['validated_target_column']} "
                                f"against {foreign_key['validated_parent_table']}."
                            )
                        values[foreign_key["validated_target_column"]] = matches[0][0]
                    cursor.execute(
                        statement,
                        tuple(values[column] for column in target_columns),
                    )
                    processed += 1
                results.append(
                    {"table": table_name, "rows_processed": processed}
                )
    return results


def load_dataframe(*, dataframe, connection_args, mapping_plan):
    connection = psycopg2.connect(**connection_args)
    try:
        return upsert_dataframe(
            connection=connection,
            dataframe=dataframe,
            mapping_plan=mapping_plan,
        )
    finally:
        connection.close()
