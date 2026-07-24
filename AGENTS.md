# AGENTS.md

## Project Overview

Flask web application for downloading and managing Mexican tax invoices (CFDIs) from the SAT (Servicio de Administracion Tributaria). Uses `satcfdi` library for SAT integration.

## Quick Start

```bash
cd "C:\Users\informatica\Documents\Proyecto nuevo\cfdisat"
iniciar.bat
# Opens http://localhost:5000
```

**Manual setup:**
```bash
pip install -r requirements.txt
pip install satcfdi lxml beautifulsoup4 pyOpenSSL
python app.py
```

## Architecture

- **Framework:** Flask + SQLAlchemy + Flask-Login
- **Database:** SQLite local / Cloud SQL produccion (auto-detect via env vars)
- **Entry point:** `app.py` → `app/__init__.py` (`create_app()`)

### Blueprints (routes)

| File | Blueprint | Purpose |
|------|-----------|---------|
| `app/auth.py` | `auth_bp` | Login/Registro |
| `app/fiel.py` | `fiel_bp` | Gestion FIEL (.cer, .key, password) |
| `app/sat.py` | `sat_bp` | Descarga CFDIs desde SAT |
| `app/dashboard.py` | `dashboard_bp` | Dashboard con estadisticas |
| `app/reportes.py` | `reportes_bp` | Reporte IVA + Exportar Excel |
| `app/admin.py` | `admin_bp` | Gestion de usuarios (solo admin) |

### Models (`app/models.py`)

- `User` - Usuarios con rol admin
- `Empresa` - Empresas asociadas a usuarios (RFC)
- `FielCredentials` - Certificados FIEL (.cer, .key encriptados)
- `CFDI` - Facturas descargadas del SAT
- `DownloadRequest` - Historial de descargas

## Key Facts

- **Admin credentials:** `admin@cfdisat.local` / `admin123` (created automatically)
- **FIEL files** stored in `app/uploads/fiel/` and `app/uploads/cfdis/`
- **Max upload:** 16MB
- **Config:** Load from `.env` or `config.py`
- **CFDI types:** I=Ingreso, E=Egreso, T=Traslado, N=Nomina, P=Pago
- **CFDI states:** vigente, cancelado

## Development

- Run with `debug=True` (Flask dev server on port 5000)
- DB tables auto-created via `db.create_all()` on startup
- No test suite present
- No linting/formatting configured

## Deployment (Google App Engine)

```bash
gcloud app deploy
```

- `app.yaml` configurado para Python 3.12
- SQLite funciona local, Cloud SQL en produccion (configurar env vars)
- URL produccion: `https://[PROJECT_ID].uc.r.appspot.com`
