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

The dashboard refreshes while tasks are pending or processing. Select a workbook name in the dashboard to open its analysis detail page, which shows the proposed normalized tables and column types, generated SQL, suggested reports and their metrics/filters/visualizations, and the original source-column profile. A task records its owner, uploaded workbook name, schema profile, database design, proposed SQL schema, report catalogue, status, and `is_deleted` flag. Set `CELERY_BROKER_URL`, `OLLAMA_MODEL`, or `ANALYZER_TASK_TIMEOUT` in the environment to override the defaults.
