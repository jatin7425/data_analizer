import json
import re

# PostgreSQL types the generated SQL may use. Anything else the model suggests
# is mapped onto one of these by normalize_design().
ALLOWED_TYPES = {
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

_TYPE_ALIASES = {
    "INT": "INTEGER",
    "INT2": "SMALLINT",
    "INT4": "INTEGER",
    "INT8": "BIGINT",
    "SERIAL": "INTEGER",
    "BIGSERIAL": "BIGINT",
    "FLOAT": "DOUBLE PRECISION",
    "FLOAT4": "REAL",
    "FLOAT8": "DOUBLE PRECISION",
    "DOUBLE": "DOUBLE PRECISION",
    "DECIMAL": "NUMERIC",
    "MONEY": "NUMERIC(14,2)",
    "BOOL": "BOOLEAN",
    "VARCHAR": "TEXT",
    "CHARACTER VARYING": "TEXT",
    "STRING": "TEXT",
    "CHAR": "TEXT",
    "DATETIME": "TIMESTAMP",
    "TIMESTAMP WITHOUT TIME ZONE": "TIMESTAMP",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ",
    "JSON": "JSONB",
}

_TRUE_WORDS = {"true", "yes", "y", "1"}
_FALSE_WORDS = {"false", "no", "n", "0", "", "none", "null"}

_FK_TABLE_KEYS = (
    "references_table", "referenced_table", "ref_table", "table",
    "target_table", "foreign_table", "parent_table",
)
_FK_COLUMN_KEYS = (
    "references_column", "referenced_column", "ref_column", "column",
    "target_column", "foreign_column", "parent_column",
)


def _identifier(name):
    """Match the identifier rules used for the generated SQL."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", str(name))
    name = re.sub(r"_+", "_", name).strip("_").lower()
    if name[:1].isdigit():
        name = "t_" + name
    return name


def _as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return None


def _normalize_type(value):
    raw = str(value or "TEXT").strip().upper()
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"\s*([(),])\s*", r"\1", raw)
    if raw in ALLOWED_TYPES:
        return raw

    match = re.fullmatch(r"(?:VARCHAR|CHARACTER VARYING|CHAR|CHARACTER|NVARCHAR)\((\d+)\)", raw)
    if match:
        length = int(match.group(1))
        for size in (50, 100, 255):
            if length <= size:
                return f"VARCHAR({size})"
        return "TEXT"

    match = re.fullmatch(r"(?:NUMERIC|DECIMAL)\((\d+)(?:,(\d+))?\)", raw)
    if match:
        precision, scale = int(match.group(1)), int(match.group(2) or 0)
        if scale == 2:
            for size in (10, 12, 14):
                if precision <= size:
                    return f"NUMERIC({size},2)"
        return "NUMERIC"

    alias = _TYPE_ALIASES.get(raw)
    if alias:
        return alias
    return "TEXT"


def _parse_reference(value):
    """Return (table, column) from the shapes small models produce, or None."""
    if value is None or value is False or value == "" or value == {} or value == []:
        return None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*\"?([\w ]+?)\"?\s*(?:\.|\()\s*\"?([\w ]+?)\"?\s*\)?\s*", value)
        return (match.group(1), match.group(2)) if match else None
    if isinstance(value, dict):
        table = next((value[key] for key in _FK_TABLE_KEYS if value.get(key)), None)
        column = next((value[key] for key in _FK_COLUMN_KEYS if value.get(key)), None)
        if table is None and isinstance(value.get("references"), (str, dict)):
            return _parse_reference(value["references"])
        if isinstance(table, str) and column is None:
            return _parse_reference(table) or (table, None)
        if isinstance(table, str):
            return (table, column if isinstance(column, str) else None)
    return None


def normalize_design(design, source_headers=None, schema_profile=None):
    """Repair or drop malformed fields in a model-proposed design, in place.

    Small local models often return a foreign key as a string, a bare boolean,
    or a dict with different key names, and use types outside ALLOWED_TYPES.
    Instead of failing the whole analysis, each field is coerced to the shape
    the SQL generator and mapping plan expect, or removed. Every change is
    recorded in design["normalization_notes"] so it can be reviewed.
    """
    notes = []
    # Sheet columns that already contain empty cells can't feed a NOT NULL column.
    headers_with_blanks = {
        str(column.get("column_name"))
        for column in schema_profile or []
        if (column.get("null_count") or 0) > 0
    }
    if source_headers is None and schema_profile:
        source_headers = [column.get("column_name") for column in schema_profile]
    header_lookup = {}
    for header in ([] if source_headers is None else list(source_headers)):
        header = str(header)
        header_lookup.setdefault(header, header)
        header_lookup.setdefault(header.strip().casefold(), header)
        header_lookup.setdefault(_identifier(header), header)
    tables = {}
    for table in design["tables"]:
        columns = {_identifier(column["column_name"]): column for column in table["columns"]}
        tables[_identifier(table["table_name"])] = (table, columns)

    for table in design["tables"]:
        table_key = _identifier(table["table_name"])
        for column in table["columns"]:
            where = f"{table['table_name']}.{column['column_name']}"

            if header_lookup:
                sources = column.get("source_columns")
                if isinstance(sources, str):
                    sources = [sources]
                matched = []
                for source in sources if isinstance(sources, list) else []:
                    text = str(source)
                    header = (
                        header_lookup.get(text)
                        or header_lookup.get(text.strip().casefold())
                        or header_lookup.get(_identifier(text))
                    )
                    if header is None:
                        notes.append(f"{where}: source column {text!r} isn't in the sheet; mapping removed.")
                    elif header not in matched:
                        matched.append(header)
                        if header != text:
                            notes.append(f"{where}: source column {text!r} matched to sheet header {header!r}.")
                column["source_columns"] = matched
                blank_sources = [header for header in matched if header in headers_with_blanks]
                if (
                    blank_sources
                    and _as_bool(column.get("nullable"), True) is False
                    and not _as_bool(column.get("primary_key"), False)
                ):
                    column["nullable"] = True
                    notes.append(
                        f"{where}: sheet column {blank_sources[0]!r} has empty cells, so it now allows empty values."
                    )
                if not matched:
                    # Imports fill columns only from the sheet, so a column with no
                    # source can't be a primary key and must allow empty values.
                    if _as_bool(column.get("primary_key"), False):
                        column["primary_key"] = False
                        notes.append(
                            f"{where}: no sheet column feeds this key, so it is no longer the primary key; "
                            "imports match rows on the table's mapped columns instead."
                        )
                    if _as_bool(column.get("nullable"), True) is False:
                        column["nullable"] = True
                        notes.append(f"{where}: no sheet column feeds it, so it now allows empty values.")

            original_type = column.get("postgres_type")
            column["postgres_type"] = _normalize_type(original_type)
            if str(original_type or "").strip().upper() != column["postgres_type"]:
                notes.append(f"{where}: type {original_type!r} changed to {column['postgres_type']}.")

            for flag, default in (("nullable", True), ("primary_key", False)):
                value = _as_bool(column.get(flag), default)
                if value is None:
                    notes.append(f"{where}: unreadable {flag} value {column.get(flag)!r}; using {default}.")
                    value = default
                column[flag] = value

            raw_fk = column.get("foreign_key")
            if raw_fk is None:
                continue
            if header_lookup and not column.get("source_columns"):
                # The importer can only fill a foreign key from a sheet column.
                notes.append(f"{where}: removed foreign key because no sheet column can fill it during imports.")
                column["foreign_key"] = None
                continue
            reference = _parse_reference(raw_fk)
            fk = None
            if reference:
                target = tables.get(_identifier(reference[0]))
                if target:
                    target_table, target_columns = target
                    column_key = _identifier(reference[1]) if reference[1] else ""
                    target_column = target_columns.get(column_key)
                    if target_column is None:
                        keys = [c for c in target_table["columns"] if c.get("primary_key") is True]
                        target_column = keys[0] if len(keys) == 1 else None
                    if target_column is not None and not (
                        _identifier(target_table["table_name"]) == table_key
                        and _identifier(target_column["column_name"]) == _identifier(column["column_name"])
                    ):
                        fk = {
                            "references_table": target_table["table_name"],
                            "references_column": target_column["column_name"],
                        }
                        if isinstance(raw_fk, dict) and raw_fk.get("validated") is True:
                            fk["validated"] = True
            if fk is None and raw_fk not in (False, "", {}, []):
                notes.append(f"{where}: removed foreign key {raw_fk!r} because it doesn't point to a column in this design.")
            column["foreign_key"] = fk

    if notes:
        design["normalization_notes"] = notes
    return notes


def build_database_design_prompt(schema_profile, row_count, column_count):
    schema_input = json.dumps(schema_profile, indent=2, ensure_ascii=False)
    return f"""
You are a senior data architect specializing in PostgreSQL.

Analyze this Excel dataset and propose a database design.

DATASET:
- Rows: {row_count}
- Columns: {column_count}

COLUMN PROFILE:
{schema_input}

Return ONLY valid JSON with this structure:

{{
  "database": "PostgreSQL",
  "reasoning": "Explanation of the design",
  "tables": [
    {{
      "table_name": "example_table in lowercase",
      "purpose": "Purpose of the table",
      "columns": [
        {{
          "column_name": "example_id",
          "source_columns": ["source_column_name"],
          "postgres_type": "TEXT",
          "nullable": true,
          "primary_key": false,
          "foreign_key": null,
          "reason": "Explanation"
        }}
      ]
    }}
  ],
  "assumptions": [],
  "data_quality_issues": [],
  "validation_checks": []
}}

RULES:
1. The top-level "tables" property is required and must be a non-empty array.
2. Identify logical entities and recommend normalized tables.
3. Preserve every source column through explicit mappings.
4. "postgres_type" must be one of: {", ".join(sorted(ALLOWED_TYPES))}.
5. "source_columns" must use the exact column_name values from the COLUMN PROFILE,
   spelled exactly as given (for example "Order ID", not "order_id").
   Every primary key must have source_columns from the profile: use a real
   identifying column (an ID, email, SKU or code), never an invented id that
   is not in the sheet. Do not assume a column is a primary key without evidence.
6. "foreign_key" is null, or an object naming a column in another table of this
   design: {{"references_table": "customers", "references_column": "customer_id"}}.
   Never put a foreign key on a table's own primary key. Treat foreign keys as
   candidates for validation.
7. Do not invent unsupported relationships.
8. Identify duplicate, redundant, and ambiguous fields.
9. Explain uncertain business meanings in assumptions.
10. Avoid unnecessary tables if the data does not justify them.
11. Return valid JSON only, without Markdown fences.
"""
