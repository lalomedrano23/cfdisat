from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Inicia sesion para acceder.'


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

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
