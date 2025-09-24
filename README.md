# Ecages

A Django-based inventory, sales, and warehouse management system.

## Features
- Inventory tracking (warehouse and salespoints)
- Sales flows (cashier, commercial, manager dashboards)
- Providers and products management
- Reports and notifications

## Quick start

### 1) Requirements
- Python 3.13 (project uses a local venv `ecages_env/`)
- SQLite (default) or your preferred DB

### 2) Create and activate a virtual environment
```bash
python3 -m venv ecages_env
source ecages_env/bin/activate
pip install -r requirements.txt
```

### 3) Environment
Create a `.env` file if needed (see `config/settings/*.py`). Typical values:
```
DJANGO_DEBUG=True
DJANGO_SECRET_KEY=change-me
ALLOWED_HOSTS=localhost,127.0.0.1
```

### 4) Migrations
```bash
python manage.py migrate
```

### 5) Create a superuser
```bash
python manage.py createsuperuser
```

### 6) Run the server
```bash
python manage.py runserver
```

### 7) Optional: load test data / barcodes
- See management commands under `apps/inventory/management/commands/` and `apps/sales/management/commands/`.

## Project structure
- `apps/` Django apps (accounts, inventory, sales, products, providers, reports, etc.)
- `config/` Django settings and URLs
- `templates/` HTML templates
- `static/` static assets

## Contributing
- Create a feature branch from `main`, then open a Pull Request.

## License
Proprietary. All rights reserved.
