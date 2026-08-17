from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Inicia sesion para acceder.'


def _ensure_fiel_columns():
    from sqlalchemy import inspect, text
    from sqlalchemy.types import LargeBinary, Float

    inspector = inspect(db.engine)

    try:
        cols = {c['name'] for c in inspector.get_columns('fiel_credentials')}
    except Exception:
        cols = set()
    if 'cer_data' not in cols or 'key_data' not in cols:
        col_type = LargeBinary().compile(dialect=db.engine.dialect)
        with db.engine.begin() as conn:
            if 'cer_data' not in cols:
                conn.execute(text(f'ALTER TABLE fiel_credentials ADD COLUMN cer_data {col_type}'))
            if 'key_data' not in cols:
                conn.execute(text(f'ALTER TABLE fiel_credentials ADD COLUMN key_data {col_type}'))

    try:
        cfdis_cols = {c['name'] for c in inspector.get_columns('cfdis')}
    except Exception:
        cfdis_cols = set()
    new_tax_cols = {'iva_trasladado', 'isr_retenido', 'iva_retenido'}
    missing = new_tax_cols - cfdis_cols
    if missing:
        col_type = Float().compile(dialect=db.engine.dialect)
        with db.engine.begin() as conn:
            for col_name in missing:
                conn.execute(text(f'ALTER TABLE cfdis ADD COLUMN {col_name} {col_type}'))
        _backfill_tax_from_xml(missing)


def _backfill_tax_from_xml(missing_cols):
    import xml.etree.ElementTree as ET
    from app.models import CFDI

    try:
        cfdis = CFDI.query.filter(CFDI.xml_content.isnot(None)).all()
    except Exception:
        return

    updated = 0
    for cf in cfdis:
        if not cf.xml_content:
            continue
        try:
            root = ET.fromstring(cf.xml_content.encode('utf-8'))
            nsmap = None
            if root.tag.startswith('{'):
                nsmap = root.tag.split('}')[0].strip('{')

            impuestos = root.find(f'{{{nsmap}}}Impuestos') if nsmap else root.find('Impuestos')
            if impuestos is None:
                continue

            iva_trasladado = 0
            isr_retenido = 0
            iva_retenido = 0

            traslados = impuestos.find(f'{{{nsmap}}}Traslados') if nsmap else impuestos.find('Traslados')
            if traslados is not None:
                for t in (traslados.findall(f'{{{nsmap}}}Traslado') if nsmap else traslados.findall('Traslado')):
                    if t.get('Impuesto') == '002':
                        iva_trasladado += float(t.get('Importe', 0))

            retenciones = impuestos.find(f'{{{nsmap}}}Retenciones') if nsmap else impuestos.find('Retenciones')
            if retenciones is not None:
                for r in (retenciones.findall(f'{{{nsmap}}}Retencion') if nsmap else retenciones.findall('Retencion')):
                    imp_code = r.get('Impuesto', '')
                    importe = float(r.get('Importe', 0))
                    if imp_code == '001':
                        isr_retenido += importe
                    elif imp_code == '002':
                        iva_retenido += importe

            if 'iva_trasladado' in missing_cols:
                cf.iva_trasladado = iva_trasladado
            if 'isr_retenido' in missing_cols:
                cf.isr_retenido = isr_retenido
            if 'iva_retenido' in missing_cols:
                cf.iva_retenido = iva_retenido
            updated += 1
        except Exception:
            continue

    if updated:
        db.session.commit()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    import os
    os.makedirs(app.config['UPLOAD_FOLDER_FIEL'], exist_ok=True)
    os.makedirs(app.config['UPLOAD_FOLDER_CFDIS'], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)

    from app.auth import auth_bp
    from app.fiel import fiel_bp
    from app.sat import sat_bp
    from app.dashboard import dashboard_bp
    from app.reportes import reportes_bp
    from app.admin import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(fiel_bp)
    app.register_blueprint(sat_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(reportes_bp)
    app.register_blueprint(admin_bp)

    with app.app_context():
        from app.models import User
        db.create_all()
        _ensure_fiel_columns()

        if not User.query.filter_by(is_admin=True).first():
            admin = User(
                email='admin@cfdisat.local',
                nombre='Administrador',
                is_admin=True,
                activo=True,
            )
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
            print('========================================')
            print('  Usuario administrador creado:')
            print('  Email:    admin@cfdisat.local')
            print('  Password: admin123')
            print('  CAMBIA ESTA CONTRASENA DESPUES!')
            print('========================================')

    return app
