"""Summarise where an analysis task is in the upload-to-import workflow.

The UI shows the workflow as five stages. Each stage has a ``state``:

- ``done``: finished successfully
- ``working``: a background job is running or queued
- ``action``: waiting for the user to do something
- ``failed``: stopped with an error or was rejected
- ``upcoming``: an earlier stage has not finished yet
"""

ACTIVE_STATES = {"working", "action", "failed"}


def _stage(key, name, state, note):
    return {"key": key, "name": name, "state": state, "note": note}


def task_stages(task, latest_import=None):
    stages = []

    if task.status == "completed":
        analysis = _stage("analysis", "Analysis", "done", "Done")
    elif task.status == "failed":
        analysis = _stage("analysis", "Analysis", "failed", "Failed")
    else:
        analysis = _stage("analysis", "Analysis", "working", task.get_status_display())
    stages.append(analysis)

    if analysis["state"] != "done":
        review = _stage("review", "Review", "upcoming", "Not started")
    elif task.review_status == "approved":
        review = _stage("review", "Review", "done", "Approved")
    elif task.review_status == "rejected":
        review = _stage("review", "Review", "failed", "Rejected")
    else:
        review = _stage("review", "Review", "action", "Needs your review")
    stages.append(review)

    if review["state"] != "done":
        mapping = _stage("mapping", "Mapping plan", "upcoming", "Not started")
    elif task.mapping_status == "completed":
        mapping = _stage("mapping", "Mapping plan", "done", "Done")
    elif task.mapping_status == "failed":
        mapping = _stage("mapping", "Mapping plan", "failed", "Failed")
    else:
        mapping = _stage("mapping", "Mapping plan", "working", "Generating")
    stages.append(mapping)

    if mapping["state"] != "done":
        database = _stage("database", "Database", "upcoming", "Not started")
    elif task.database_status == "completed":
        database = _stage("database", "Database", "done", "Ready")
    elif task.database_status == "failed":
        database = _stage("database", "Database", "failed", "Setup failed")
    elif task.database_status in ("pending", "processing"):
        database = _stage("database", "Database", "working", "Setting up")
    else:
        database = _stage("database", "Database", "action", "Connect a database")
    stages.append(database)

    if database["state"] != "done":
        imports = _stage("imports", "Imports", "upcoming", "Not started")
    elif latest_import is None:
        imports = _stage("imports", "Imports", "action", "Import a workbook")
    elif latest_import.status in ("pending", "processing"):
        imports = _stage("imports", "Imports", "working", "Importing")
    elif latest_import.status == "completed":
        imports = _stage("imports", "Imports", "done", "Last import succeeded")
    elif latest_import.status == "schema_mismatch":
        imports = _stage("imports", "Imports", "failed", "Columns didn't match")
    else:
        imports = _stage("imports", "Imports", "failed", "Last import failed")
    stages.append(imports)

    current = next(
        (stage for stage in stages if stage["state"] in ACTIVE_STATES),
        stages[-1],
    )
    for number, stage in enumerate(stages, start=1):
        stage["number"] = number
        stage["is_current"] = stage is current
    return stages


def current_stage(stages):
    return next(stage for stage in stages if stage["is_current"])
