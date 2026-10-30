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

## Public deployment checklist

This repository is currently configured for local development. These steps
describe what to prepare before making it reachable from the public internet;
they are not a one-command deployment recipe. Choose a hosting provider and
deployment architecture before carrying them out.

### 1. Choose and secure the host

- Choose a Linux VPS or managed container host with enough memory and CPU for
  Django, Celery, PostgreSQL, Redis, and Ollama. Ollama model inference can be
  resource-intensive; alternatively, use a separately secured model service
  that the worker can reach.
- Obtain a domain name and point its DNS records to the web host.
- Keep the operating system, runtime, and dependencies patched. Use a
  dedicated, unprivileged service account for the application.
- Configure the host firewall to expose only the required web ports (normally
  HTTP/HTTPS, with HTTP redirected to HTTPS). Do not expose PostgreSQL, Redis,
  or Ollama's API directly to the internet.

### 2. Add production Django settings before deployment

The current `data_analizer/settings.py` has `DEBUG=True`, a hard-coded
development `SECRET_KEY`, and `ALLOWED_HOSTS=[]`. Production deployment must
not use those settings. Before launching publicly:

- Load `SECRET_KEY` from the host's secret manager or environment; generate a
  new, private value for production and never commit it.
- Set `DEBUG=False` and configure `ALLOWED_HOSTS` for the public domain(s).
- Configure `CSRF_TRUSTED_ORIGINS` for the HTTPS origin(s).
- Enable secure session and CSRF cookies and HTTPS-related security settings.
  Enable HSTS only after HTTPS is confirmed working.
- Run `python manage.py check --deploy` and address the deployment warnings.
- Decide whether public user registration is intended. If not, disable or
  restrict sign-up before launch.

These settings are not fully implemented in the current project; update and
test the deployment configuration before treating the checklist as complete.

### 3. Configure secrets and services

Provide production values through the hosting platform's secret manager or
environment configuration, not a committed `.env` file. Configure at least:

- Django `SECRET_KEY` (after production settings support it)
- `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD`
- `POSTGRES_HOST` and `POSTGRES_PORT`
- `CELERY_BROKER_URL`
- `OLLAMA_MODEL`
- `ANALYZER_CREDENTIAL_ENCRYPTION_KEY`

Generate a new Fernet key for production using the command in the local setup
section. Keep it private, back it up securely, and use the exact same value in
the web and worker processes. Changing or losing this key prevents decryption
of database passwords stored by the application.

Use managed PostgreSQL and Redis or run them on private networks with
persistent storage and access controls. The development `docker-compose.yml`
publishes PostgreSQL on host port `5435` and Redis on `6379`; do not expose
those ports publicly in production. Restrict PostgreSQL privileges to what
the application and database-provisioning workflow need. Keep Ollama private
and reachable only by the worker.

### 4. Run production web and background processes

- Use a production WSGI server (for example, Gunicorn) behind a reverse proxy
  such as Nginx or Caddy. `manage.py runserver` is for development only.
- Terminate TLS at the reverse proxy or hosting platform and redirect HTTP to
  HTTPS. Configure proxy headers and Django security settings for that
  deployment.
- Run one or more Celery workers as supervised services (such as
  `systemd` or the hosting platform's process manager). The worker needs the
  same application code, secrets, database access, Redis access, and Ollama
  access as the web process.
- Configure automatic restarts, structured logs, monitoring, and alerts for
  the web process, worker, database, Redis, and model service.
- Set upload-size limits at the reverse proxy/platform and ensure worker
  timeouts and memory limits fit the expected Excel file sizes and model work.

The current requirements do not include a production WSGI server, and the
project does not yet include production process or reverse-proxy
configuration. Add and test these as part of deployment preparation.

### 5. Prepare persistent data, static files, and backups

- Store uploaded files on durable private storage. The current application
  writes uploads under `MEDIA_ROOT`; local/container filesystem storage can be
  lost when an instance is replaced unless a persistent volume or object
  storage is configured.
- Configure `STATIC_ROOT`, then run:

  ```bash
  python manage.py collectstatic --noinput
  ```

  Serve static files through the reverse proxy, a static-file service, or a
  supported storage backend.
- Back up PostgreSQL and uploaded files regularly. Protect backups because
  they may contain user data and encrypted connection credentials. Test
  restoring them, and keep the production Fernet key backed up separately.
- Apply Django migrations during deployment before starting new web and
  worker code:

  ```bash
  python manage.py migrate
  ```

### 6. Verify before opening access

1. Deploy to a private/staging URL first and verify HTTPS, login, uploads,
   analysis jobs, database setup, and imports.
2. Confirm the web server and Celery worker use the same release and
   environment, and verify workers restart after code updates.
3. Check `python manage.py check --deploy`, application logs, worker logs,
   database connectivity, backups, and restore procedures.
4. Open public access only after the staging checks pass and firewall rules
   expose only the intended web endpoints.

The app handles uploaded spreadsheets and stores database connection
credentials (encrypted with the Fernet key). Treat uploads, database access,
logs, backups, and encryption keys as sensitive production data.
