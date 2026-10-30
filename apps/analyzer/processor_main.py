
import os
import re
import json
import pandas as pd

import ollama

from processor_utils import build_database_design_prompt

# ============================================================
# CONFIGURATION
# ============================================================

FILE_PATH = os.getenv("SOURCE_EXCEL_FILE", "Vrinda Store Data Analysis.xlsx")
SHEET_NAME_VALUE = os.getenv("SOURCE_SHEET_NAME", "0")
try:
    SHEET_NAME = int(SHEET_NAME_VALUE)
except ValueError:
    SHEET_NAME = SHEET_NAME_VALUE

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
OUTPUT_JSON = os.getenv("DATABASE_DESIGN_FILE", "database_design.json")
OUTPUT_SQL = os.getenv("PROPOSED_SCHEMA_FILE", "proposed_schema.sql")
OUTPUT_PROFILE = os.getenv("SCHEMA_PROFILE_FILE", "excel_schema_profile.json")
OUTPUT_REPORTS = os.getenv("REPORT_CATALOGUE_FILE", "report_catalogue.json")
OUTPUT_TO_DATABASE = os.getenv("ANALYZER_OUTPUT_TO_DATABASE") == "1"
DATABASE_OUTPUT_MARKER = "ANALYZER_DATABASE_OUTPUT:"
database_outputs = {}


def save_json_output(path, value):
    if OUTPUT_TO_DATABASE:
        database_outputs[os.path.basename(path)] = value
        return

    with open(path, "w", encoding="utf-8") as file:
        json.dump(value, file, indent=2, ensure_ascii=False)


def save_text_output(path, value):
    if OUTPUT_TO_DATABASE:
        database_outputs[os.path.basename(path)] = value
        return

    with open(path, "w", encoding="utf-8") as file:
        file.write(value)


# ============================================================
# STEP 1: READ EXCEL
# ============================================================

print("\n[1/6] Reading Excel file...")

xls = pd.ExcelFile(FILE_PATH)

print("Available sheets:", xls.sheet_names)

df = pd.read_excel(
    xls,
    sheet_name=SHEET_NAME
)
df.columns = [str(column).strip() for column in df.columns]

print("Total rows:", len(df))
print("Total columns:", len(df.columns))

if df.empty:
    raise ValueError("The selected Excel sheet is empty.")

print("\nOriginal column names:")
print(df.columns.tolist())


# ============================================================
# STEP 2: PROFILE THE DATA AND INFER BASIC SCHEMA
# ============================================================

print("\n[2/6] Generating schema profile...")


def build_schema_profile(dataframe):
    profile = []

    for column in dataframe.columns:
        series = dataframe[column]

        # Limit samples to avoid sending too much data
        examples = (
            series.dropna()
            .astype(str)
            .drop_duplicates()
            .head(5)
            .tolist()
        )

        profile.append({
            "column_name": str(column),
            "pandas_dtype": str(series.dtype),
            "null_count": int(series.isna().sum()),
            "null_percentage": round(
                float(series.isna().mean() * 100), 2
            ),
            "unique_count": int(series.nunique()),
            "sample_values": examples
        })

    return profile


schema_profile = build_schema_profile(df)
save_json_output(OUTPUT_PROFILE, schema_profile)

print(
    "Schema profile passed to database writer."
    if OUTPUT_TO_DATABASE
    else f"Schema profile saved: {OUTPUT_PROFILE}"
)
print(json.dumps(schema_profile, indent=2, ensure_ascii=False))


# ============================================================
# STEP 3: PREPARE THE OLLAMA CLIENT
# ============================================================

print(f"\n[3/6] Using Ollama model {OLLAMA_MODEL}...")

# ============================================================
# STEP 4: ASK THE LLM TO DESIGN THE DATABASE
# ============================================================

print("\n[4/6] Asking the LLM to design the database...")

prompt = build_database_design_prompt(
    schema_profile,
    row_count=len(df),
    column_count=len(df.columns),
)

# response = client.chat.completions.create(
#     model=MODEL_ID,
#     messages=[
#         {
#             "role": "system",
#             "content": (
#                 "You are an expert database architect. "
#                 "Return valid JSON only."
#             )
#         },
#         {
#             "role": "user",
#             "content": prompt
#         }
#     ],
#     max_tokens=4000,
#     temperature=0.1
# )

# llm_output = response.choices[0].message.content

# if not llm_output:
#     raise ValueError("The LLM returned an empty response.")

response = ollama.chat(
    model=OLLAMA_MODEL,
    messages=[
        {
            "role": "system",
            "content": "You are a database schema design expert. Return valid JSON only."
        },
        {
            "role": "user",
            "content": prompt
        }
    ],
    format="json",
    options={"temperature": 0.1}
)

llm_output = response["message"]["content"]

print("\nRaw LLM response:")
print(llm_output)


# ============================================================
# STEP 5: PARSE AND SAVE THE PROPOSED DATABASE DESIGN
# ============================================================

print("\n[5/6] Parsing database design...")


def parse_llm_json(text):
    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        text = "\n".join(lines)

    result = json.loads(text)

    if not isinstance(result, dict):
        raise ValueError("Ollama returned a database design that is not a JSON object.")

    tables = result.get("tables")
    if not isinstance(tables, list) or not tables:
        returned_keys = ", ".join(sorted(result)) or "none"
        raise ValueError(
            "Ollama returned an invalid database design: expected a non-empty "
            f"top-level 'tables' array; returned keys: {returned_keys}."
        )

    table_names = set()

    for table in tables:
        if not isinstance(table, dict):
            raise ValueError("Each item in the tables array must be an object.")
        if not table.get("table_name"):
            raise ValueError("A table has no name.")

        if not isinstance(table.get("columns"), list):
            raise ValueError(
                f"Invalid columns for {table['table_name']}"
            )

        normalized_table = safe_identifier(table["table_name"])

        if normalized_table in table_names:
            raise ValueError(
                f"Duplicate table name: {normalized_table}"
            )

        table_names.add(normalized_table)

        column_names = set()

        for column in table["columns"]:
            if not isinstance(column, dict):
                raise ValueError(
                    f"Each column in table {table['table_name']} must be an object."
                )
            if not column.get("column_name"):
                raise ValueError("A column has no name.")

            normalized_column = safe_identifier(
                column["column_name"]
            )

            if normalized_column in column_names:
                raise ValueError(
                    f"Duplicate column {normalized_column} "
                    f"in table {table['table_name']}"
                )

            column_names.add(normalized_column)

    return result


def safe_identifier(name):
    name = re.sub(r"[^a-zA-Z0-9_]", "_", str(name))
    name = re.sub(r"_+", "_", name).strip("_").lower()

    if not name:
        raise ValueError("Empty SQL identifier.")

    if name[0].isdigit():
        name = "t_" + name

    return name


db_design = parse_llm_json(llm_output)
save_json_output(OUTPUT_JSON, db_design)

print(
    "Database design passed to database writer."
    if OUTPUT_TO_DATABASE
    else f"Database design saved: {OUTPUT_JSON}"
)

print("\nProposed tables:")

for table in db_design["tables"]:
    print(f"\nTable: {table['table_name']}")
    print("Purpose:", table.get("purpose", ""))

    for column in table["columns"]:
        print(
            f"  {column['column_name']} "
            f"-> {column['postgres_type']}"
        )


# ============================================================
# STEP 6: GENERATE REVIEWABLE POSTGRESQL DDL
# ============================================================

print("\n[6/6] Generating PostgreSQL SQL...")


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
    "VARCHAR(255)"
}


def generate_postgres_ddl(design):
    statements = []

    # Build table and column lookup for relationship validation
    table_lookup = {}

    for table in design["tables"]:
        table_name = safe_identifier(table["table_name"])
        columns = {}

        for column in table["columns"]:
            column_name = safe_identifier(column["column_name"])

            if column_name in columns:
                raise ValueError(
                    f"Duplicate column: {table_name}.{column_name}"
                )

            columns[column_name] = column

        table_lookup[table_name] = columns

    for table in design["tables"]:
        table_name = safe_identifier(table["table_name"])
        definitions = []
        primary_keys = []
        foreign_keys = []
        seen_columns = set()

        for column in table["columns"]:
            column_name = safe_identifier(column["column_name"])

            if column_name in seen_columns:
                raise ValueError(
                    f"Duplicate column: {table_name}.{column_name}"
                )

            seen_columns.add(column_name)

            sql_type = str(
                column.get("postgres_type", "TEXT")
            ).upper().strip()

            # Normalize common PostgreSQL type aliases
            TYPE_ALIASES = {
                "INT": "INTEGER",
                "INT4": "INTEGER",
                "INT8": "BIGINT",
                "FLOAT": "DOUBLE PRECISION",
                "BOOL": "BOOLEAN",
                "VARCHAR": "TEXT",
            }

            sql_type = TYPE_ALIASES.get(sql_type, sql_type)

            if sql_type not in ALLOWED_TYPES:
                raise ValueError(
                    f"Unapproved SQL type {sql_type} for "
                    f"{table_name}.{column_name}. "
                    "Review the type before adding it to ALLOWED_TYPES."
                )

            nullable = column.get("nullable", True)
            is_primary_key = column.get("primary_key", False)

            if not isinstance(nullable, bool):
                raise ValueError(
                    f"Invalid nullable flag: {table_name}.{column_name}"
                )

            if not isinstance(is_primary_key, bool):
                raise ValueError(
                    f"Invalid primary_key flag: "
                    f"{table_name}.{column_name}"
                )

            definition = f'    "{column_name}" {sql_type}'

            if not nullable or is_primary_key:
                definition += " NOT NULL"

            definitions.append(definition)

            if is_primary_key:
                primary_keys.append(column_name)

            fk = column.get("foreign_key")

            # Foreign keys are generated only when the LLM provides
            # a structured reference and the target exists.
            if fk is not None:
                if not isinstance(fk, dict):
                    raise ValueError(
                        f"Invalid foreign_key structure for "
                        f"{table_name}.{column_name}"
                    )

                if fk.get("validated") is not True:
                    print(
                        "Skipping unvalidated foreign key:",
                        table_name,
                        column_name
                    )
                    continue

                target_table = safe_identifier(
                    fk.get("references_table", "")
                )
                target_column = safe_identifier(
                    fk.get("references_column", "")
                )

                if target_table not in table_lookup:
                    raise ValueError(
                        f"Unknown referenced table: {target_table}"
                    )

                if target_column not in table_lookup[target_table]:
                    raise ValueError(
                        f"Unknown referenced column: "
                        f"{target_table}.{target_column}"
                    )

                foreign_keys.append(
                    (column_name, target_table, target_column)
                )

        if primary_keys:
            pk_sql = ", ".join(
                f'"{key}"' for key in primary_keys
            )

            definitions.append(
                f"    PRIMARY KEY ({pk_sql})"
            )

        for source_col, target_table, target_col in foreign_keys:
            definitions.append(
                f'    FOREIGN KEY ("{source_col}") '
                f'REFERENCES "{target_table}" ("{target_col}")'
            )

        statement = (
            f'CREATE TABLE "{table_name}" (\n'
            + ",\n".join(definitions)
            + "\n);"
        )

        statements.append(statement)

    return "\n\n".join(statements)


ddl = generate_postgres_ddl(db_design)
save_text_output(OUTPUT_SQL, ddl)

print("\nGenerated SQL:")
print(ddl)


#======================================================
# Suggested reports based on the data columns and types
#======================================================

def suggest_reports(schema_profile, db_design):
    print("\n[7/7] Suggesting reports based on the schema...")
    prompt = f"""
You are a business intelligence architect.

Analyze the validated database schema and suggest
useful business reports supported by the available data.

Schema profile:
{json.dumps(schema_profile, indent=2)}

Database design:
{json.dumps(db_design, indent=2)}

Rules:
1. Suggest reports based on actual tables and columns.
2. Do not invent missing columns or relationships.
3. Identify metrics, dimensions and useful filters.
4. Identify suitable visualizations.
5. Mark feasibility as:
   - available
   - requires_data
   - requires_validation
6. Explain missing fields when a report cannot be
   generated reliably.
7. Include sales, product, customer, operational
   or other relevant report categories as appropriate
   for the dataset.
8. Return valid JSON only.

Return this structure:
{{
  "reports": [
    {{
      "report_id": "example_report",
      "report_name": "Example Report",
      "category": "Analytics",
      "description": "Report purpose",
      "required_tables": [],
      "required_columns": [],
      "metrics": [],
      "filters": [],
      "visualization": "bar_chart",
      "feasibility": "available",
      "missing_fields": []
    }}
  ]
}}
"""

    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a business intelligence "
                    "expert. Return valid JSON only."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        format="json",
        options={"temperature": 0.1}
    )

    result = json.loads(
        response["message"]["content"]
    )

    return result

report_catalogue = suggest_reports(
    schema_profile,
    db_design
)

save_json_output(OUTPUT_REPORTS, report_catalogue)

print(
    "Report catalogue passed to database writer."
    if OUTPUT_TO_DATABASE
    else f"Suggested reports saved to {OUTPUT_REPORTS}"
)

if OUTPUT_TO_DATABASE:
    print("\nGenerated outputs returned for database storage.")
else:
    print("\nFiles generated successfully:")
    print(f"1. {OUTPUT_PROFILE}")
    print(f"2. {OUTPUT_JSON}")
    print(f"3. {OUTPUT_SQL}")
    print(f"4. {OUTPUT_REPORTS}")

print(
    "\nReview the proposed tables, data types, primary keys, "
    "and relationships before executing the SQL."
)

if OUTPUT_TO_DATABASE:
    output_names = {
        "schema_profile": OUTPUT_PROFILE,
        "database_design": OUTPUT_JSON,
        "proposed_schema": OUTPUT_SQL,
        "report_catalogue": OUTPUT_REPORTS,
    }
    print(
        DATABASE_OUTPUT_MARKER
        + json.dumps(
            {
                key: database_outputs[os.path.basename(filename)]
                for key, filename in output_names.items()
            },
            ensure_ascii=False,
        )
    )
