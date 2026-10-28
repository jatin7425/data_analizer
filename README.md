# data_analizer

A Django application for analyzing Excel workbooks and loading normalized
records into PostgreSQL. Django stores task and import status, while Celery
workers run analysis, database setup, and imports asynchronously. Ollama
provides the local language model used to suggest a database design and reports.

## Requirements

- Python 3.12
- Docker with Docker Compose, or separately running PostgreSQL and Redis
- Ollama, with the model used by the application downloaded locally

The commands below assume Linux/macOS, a local development setup, and a
terminal opened at the repository root.

## 1. Create a Python environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

In a new terminal, activate the environment before running Django or Celery:

```bash
cd /path/to/data_analizer
source .venv/bin/activate
```

## 2. Configure local services and environment

Create a `.env` file in the repository root. It is ignored by Git; do not
commit passwords or encryption keys.

```dotenv
POSTGRES_DB=data_analizer
POSTGRES_USER=data_analizer
POSTGRES_PASSWORD=replace-with-a-local-password
POSTGRES_HOST=localhost
POSTGRES_PORT=5435

CELERY_BROKER_URL=redis://localhost:6379/0
OLLAMA_MODEL=qwen2.5:3b
ANALYZER_TASK_TIMEOUT=1800
ANALYZER_CREDENTIAL_ENCRYPTION_KEY=replace-with-a-generated-fernet-key
```

Generate a Fernet key and copy the output into
`ANALYZER_CREDENTIAL_ENCRYPTION_KEY`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep the same key in the environment used by both Django and Celery. Losing or
changing it prevents decrypting database passwords already saved by the
application.

Start PostgreSQL and Redis using the `.env` values:

```bash
docker compose up -d db redis
```

The Compose file publishes PostgreSQL on port `5435` and Redis on port `6379`.
If you use services installed outside Docker, configure the corresponding
host, port, database name, user, and password in `.env` instead.

## 3. Start Ollama and download the model

Start Ollama if it is not already running as a service:

```bash
ollama serve
```

In another terminal, download the configured model:

```bash
ollama pull qwen2.5:3b
```

If `OLLAMA_MODEL` is set to a different model in `.env`, pull that model
instead. Ollama must be reachable by the Celery worker.

## 4. Set up Django

After PostgreSQL is running and `.env` is configured, apply migrations and
check the project configuration:

```bash
python manage.py migrate
python manage.py check
```

Optional: create a Django admin user:

```bash
python manage.py createsuperuser
```

## 5. Run the web application and Celery worker

Run each process in a separate terminal from the repository root, with the
Python environment activated in each terminal.

Terminal 1 — Django web server:

```bash
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. Create an application account at
<http://127.0.0.1:8000/accounts/signup/> and sign in.

Terminal 2 — Celery worker:

```bash
celery -A data_analizer worker --loglevel=info
```

Leave PostgreSQL, Redis, Ollama, the Django server, and the Celery worker
running while using the application. Django queues background jobs in Redis;
the worker must be running to process them.

## 6. Analyze a workbook and import data

1. Sign in and upload an `.xlsx` or `.xls` workbook from the dashboard.
2. Wait for the analysis task to finish. Open it to review the proposed
   normalized tables, column mappings, SQL, and report suggestions.
3. Approve the analysis. The application generates and saves a mapping plan.
4. Enter the PostgreSQL server credentials and destination database name, then
   run database setup. The configured PostgreSQL user needs permission to
   create the destination database if it does not exist, and to create tables
   and columns in it.
5. After setup completes, upload a workbook to import its first worksheet.
   The worksheet must have the same column names and inferred data types as
   the original workbook. Column order may differ; surrounding whitespace in
   headers is ignored.
6. Review the import status and any reported schema or data errors on the task
   detail page.

Imports use mapped primary keys as upsert keys when the mapping supplies them.
For a table without a source-mapped primary key, all its mapped columns form a
composite upsert key. Such keys must be non-null and unique in existing target
rows; the importer creates a unique index to support PostgreSQL `ON CONFLICT`.
Only fields in the mapping plan are imported.

## Tests and schema changes

Run the analyzer tests:

```bash
python manage.py test apps.analyzer.tests
```

After pulling code that adds or removes Django model fields, apply migrations
before using the web app or worker:

```bash
python manage.py migrate
```

If a model change has no migration yet, create one and commit it with the
change:

```bash
python manage.py makemigrations
python manage.py migrate
```

When code changes, stop and restart both the Django development server and
Celery worker so each process loads the updated code. In particular, restart
the worker after deploying an importer or model change.

## Troubleshooting

- **A task stays pending:** confirm Redis and the Celery worker are running,
  and that `CELERY_BROKER_URL` points to the Redis service.
- **Database connection fails:** verify `.env` values, PostgreSQL availability,
  and whether the destination database credentials refer to the PostgreSQL
  server rather than the Django application's own database.
- **A column error mentions a field that was removed:** run
  `python manage.py migrate`, then restart the web server and Celery worker.
- **Ollama/model errors:** make sure `ollama serve` is available and the value
  of `OLLAMA_MODEL` has been downloaded with `ollama pull`.
- **Excel schema mismatch:** the import expects the same source headers and
  inferred types as the workbook used for the original analysis. Check the
  task's schema mismatch details before retrying.

## Configuration

Settings are defined in `data_analizer/settings.py`. The application reads
PostgreSQL connection settings, the Celery broker URL, Ollama model name, task
timeout, and credential-encryption key from environment variables (including
the root `.env` file). Defaults for optional variables are defined in settings.

The checked-in Django settings are for local development (`DEBUG=True`). Do
not deploy them as-is to a public or production environment. Use a
production-grade secret key, secure environment-specific settings, and
properly restricted database credentials before deployment.
