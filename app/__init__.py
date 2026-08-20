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

    new_text_cols = {'conceptos_json', 'pdf_path'}
    missing_text = new_text_cols - cfdis_cols
    if missing_text:
        with db.engine.begin() as conn:
            if 'conceptos_json' in missing_text:
                conn.execute(text('ALTER TABLE cfdis ADD COLUMN conceptos_json TEXT'))
            if 'pdf_path' in missing_text:
                conn.execute(text('ALTER TABLE cfdis ADD COLUMN pdf_path VARCHAR(500)'))
        _backfill_conceptos_from_xml()


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


def _backfill_conceptos_from_xml():
    import json
    import xml.etree.ElementTree as ET
    from app.models import CFDI

    try:
        cfdis = CFDI.query.filter(
            CFDI.xml_content.isnot(None),
            CFDI.conceptos_json.is_(None)
        ).all()
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

            conceptos_elem = root.find(f'{{{nsmap}}}Conceptos') if nsmap else root.find('Conceptos')
            if conceptos_elem is None:
                continue

            conceptos = []
            for concepto in (conceptos_elem.findall(f'{{{nsmap}}}Concepto') if nsmap else conceptos_elem.findall('Concepto')):
                conceptos.append({
                    'claveProdServ': concepto.get('ClaveProdServ', ''),
                    'cantidad': float(concepto.get('Cantidad', 0)),
                    'claveUnidad': concepto.get('ClaveUnidad', ''),
                    'descripcion': concepto.get('Descripcion', ''),
                    'valorUnitario': float(concepto.get('ValorUnitario', 0)),
                    'importe': float(concepto.get('Importe', 0)),
                })

            if conceptos:
                cf.conceptos_json = json.dumps(conceptos, ensure_ascii=False)
                updated += 1
        except Exception:
            continue

    if updated:
        db.session.commit()


def create_app():
    import logging
    import os

    app = Flask(__name__)
    app.config.from_object(Config)

    logging.basicConfig(level=logging.INFO)
    app.logger.setLevel(logging.INFO)

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

    @app.errorhandler(500)
    def internal_error(e):
        from flask import render_template, request
        app.logger.error(f'500 error: {request.method} {request.path} - {e}')
        try:
            db.session.rollback()
        except Exception:
            db.session.remove()
        return render_template('500.html'), 500

    @app.errorhandler(404)
    def not_found(e):
        from flask import render_template
        return render_template('404.html'), 404

    @app.route('/health')
    def health():
        try:
            db.session.execute(db.text('SELECT 1'))
            return 'OK', 200
        except Exception as e:
            app.logger.error(f'Health check failed: {e}')
            return 'DB ERROR', 500

    with app.app_context():
        from app.models import User
        try:
            db.create_all()
            _ensure_fiel_columns()
        except Exception as e:
            app.logger.warning(f'Migration error: {e}')

        try:
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
                app.logger.info('Admin user created: admin@cfdisat.local')
        except Exception as e:
            app.logger.warning(f'Admin creation error: {e}')

    return app
