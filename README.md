# data_analizer

Analyze uploaded Excel files asynchronously. Each upload creates an `AnalysisTask` row in the `task` table. The worker stores the schema profile, database design, proposed SQL schema, and report catalogue in separate columns on that row and updates its status. When run by the worker, the processor returns these results directly to Django for database storage rather than writing generated artifact files.

## Run the application and worker

Install the Python dependencies, then start PostgreSQL and Redis:

```bash
pip install -r requirements.txt
docker compose up -d db redis
python manage.py migrate
```

The analysis script uses Ollama. Start Ollama and make the configured model available (defaults to `qwen2.5:3b`):

```bash
ollama serve
ollama pull qwen2.5:3b
```

Run the Django web server and the Celery worker in separate terminals:

```bash
python manage.py runserver
celery -A data_analizer worker --loglevel=info
```

The dashboard refreshes while tasks are pending or processing. Select a workbook name in the dashboard to open its analysis detail page, which shows the proposed normalized tables and column types, generated SQL, suggested reports and their metrics/filters/visualizations, and the original source-column profile. Once analysis is complete, approve or reject it. Approval queues mapping-plan generation; the resulting JSON is stored on the task row and shown on the detail page. After the mapping plan completes, enter PostgreSQL server credentials and a target database name. The worker connects to PostgreSQL, creates the database if absent, creates missing tables, and adds missing columns. Existing columns with mismatched types or constraints are reported and left unchanged; no tables or data are dropped or rewritten. Passwords are encrypted using `ANALYZER_CREDENTIAL_ENCRYPTION_KEY` and cleared after the worker attempt.

Generate and set a Fernet key in the application and worker environments (the same value is required by both):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set that value as `ANALYZER_CREDENTIAL_ENCRYPTION_KEY` in your environment or `.env`; do not commit it. The PostgreSQL user needs permission to create databases when the requested database does not exist, and sufficient privileges to create tables and columns in the destination database. Set `CELERY_BROKER_URL`, `OLLAMA_MODEL`, or `ANALYZER_TASK_TIMEOUT` to override other defaults.
