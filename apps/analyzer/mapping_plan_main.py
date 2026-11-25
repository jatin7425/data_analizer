"""
Generate a REVIEWABLE data_mapping_plan.json from database_design.json.
This does not insert data. Review the plan before enabling the loader.
"""
import json
import os
from pathlib import Path


DESIGN_FILE = Path(os.getenv("DATABASE_DESIGN_FILE", "database_design.json"))
PLAN_FILE = Path(os.getenv("MAPPING_PLAN_FILE", "data_mapping_plan.json"))
SOURCE_FILE = os.getenv("SOURCE_EXCEL_FILE", "Vrinda Store Data Analysis.xlsx")
SHEET_NAME = os.getenv("SOURCE_SHEET_NAME", "0")


def source_columns(column):
    value = column.get("source_columns", [])
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        raise ValueError(
            f"source_columns must be a list for {column.get('column_name')}"
        )
    return [
        str(v).strip()
        for v in value
        if v is not None and str(v).strip()
    ]


def main():
    if not DESIGN_FILE.exists():
        raise FileNotFoundError(f"Cannot find {DESIGN_FILE.resolve()}")

    design = json.loads(DESIGN_FILE.read_text(encoding="utf-8"))
    tables = design.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValueError("database_design.json must contain a non-empty 'tables' list.")

    table_lookup = {}
    for table in tables:
        name = table.get("table_name")
        if not name:
            raise ValueError("Every table must have table_name.")
        table_lookup[name] = table

    plan_tables = []
    for table in tables:
        table_name = table["table_name"]
        columns = table.get("columns", [])
        planned_columns = []
        primary_key_source_columns = []
        foreign_keys = []

        for col in columns:
            col_name = col.get("column_name")
            if not col_name:
                raise ValueError(f"Column without column_name in table {table_name}")

            src = source_columns(col)
            is_pk = bool(col.get("primary_key", False))
            fk = col.get("foreign_key")

            planned_columns.append({
                "target_column": col_name,
                "source_columns": src,
                "primary_key": is_pk,
                "foreign_key": fk if isinstance(fk, dict) else None,
                "include": bool(src),
                "notes": (
                    "Review source-to-target meaning before loading."
                    if src else
                    "No source column specified; likely database-generated or needs manual mapping."
                )
            })

            if is_pk and src:
                primary_key_source_columns.append(col_name)

            if isinstance(fk, dict) and fk.get("references_table") and fk.get("references_column"):
                ref_table = fk["references_table"]
                ref_col = fk["references_column"]
                ref_table_def = table_lookup.get(ref_table, {})
                ref_col_def = next(
                    (c for c in ref_table_def.get("columns", [])
                     if c.get("column_name") == ref_col),
                    None
                )
                ref_src = source_columns(ref_col_def or {})
                lookup_candidates = [
                    c for c in ref_table_def.get("columns", [])
                    if source_columns(c) == src and source_columns(c)
                    and c.get("column_name") != ref_col
                ] if src else []

                if ref_src and src and ref_src == src:
                    strategy = "direct"
                    lookup_by = []
                elif len(lookup_candidates) == 1:
                    strategy = "lookup"
                    lookup_by = [lookup_candidates[0]["column_name"]]
                else:
                    strategy = "needs_review"
                    lookup_by = []

                foreign_keys.append({
                    "column": col_name,
                    "references_table": ref_table,
                    "references_column": ref_col,
                    "source_columns": src,
                    "strategy": strategy,
                    "lookup_by": lookup_by,
                    "validated_in_design": fk.get("validated") is True,
                    "review_required": True,
                    "notes": (
                        "Direct source value can be used only if source and target key semantics truly match."
                        if strategy == "direct" else
                        "Resolve generated target key by looking up the parent row using lookup_by."
                        if strategy == "lookup" else
                        "Set strategy and lookup_by manually; the correct relationship could not be inferred safely."
                    )
                })

        plan_tables.append({
            "table_name": table_name,
            "columns": planned_columns,
            # These are suggestions only. Edit them to reflect the intended uniqueness
            # of each normalized entity (e.g. composite keys for order-line tables).
            "deduplicate_by": primary_key_source_columns,
            "allow_duplicates": False,
            "foreign_keys": foreign_keys,
            "review_required": True
        })

    try:
        sheet_value = int(SHEET_NAME)
    except ValueError:
        sheet_value = SHEET_NAME

    plan = {
        "source_file": SOURCE_FILE,
        "sheet_name": sheet_value,
        "approved": False,
        "notes": [
            "Review all column mappings and foreign keys before loading.",
            "Set approved=true only after reviewing this plan.",
            "Set deduplicate_by to the intended natural/business key or source-backed primary key for each table.",
            "If a table intentionally accepts duplicate rows, set allow_duplicates=true explicitly.",
            "Foreign-key lookup strategies must be reviewed against the real schema."
        ],
        "tables": plan_tables
    }

    PLAN_FILE.write_text(
        json.dumps(plan, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    print(f"Created review plan: {PLAN_FILE.resolve()}")
    print("No database data was changed.")
    print("Review data_mapping_plan.json before running load_normalized_data.py.")


if __name__ == "__main__":
    main()
