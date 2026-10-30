import json


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
4. Suggest appropriate PostgreSQL types.
5. Do not assume a column is a primary key without evidence.
6. Foreign keys must be treated as candidates for validation.
7. Do not invent unsupported relationships.
8. Identify duplicate, redundant, and ambiguous fields.
9. Explain uncertain business meanings in assumptions.
10. Avoid unnecessary tables if the data does not justify them.
11. Return valid JSON only, without Markdown fences.
"""
