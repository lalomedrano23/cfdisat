from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from app import db, login_manager


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    nombre = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    activo = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    empresas = db.relationship('Empresa', backref='owner', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


class Empresa(db.Model):
    __tablename__ = 'empresas'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    rfc = db.Column(db.String(13), nullable=False)
    razon_social = db.Column(db.String(300), nullable=False)
    activa = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    fiel = db.relationship('FielCredentials', backref='empresa', uselist=False)
    cfdis = db.relationship('CFDI', backref='empresa', lazy=True)


class FielCredentials(db.Model):
    __tablename__ = 'fiel_credentials'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=False)
    cer_filename = db.Column(db.String(256), nullable=False)
    key_filename = db.Column(db.String(256), nullable=False)
    password_encrypted = db.Column(db.String(512), nullable=False)
    rfc = db.Column(db.String(13), nullable=False)
    nombre = db.Column(db.String(300))
    fecha_vigencia = db.Column(db.DateTime)
    activa = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CFDI(db.Model):
    __tablename__ = 'cfdis'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=False)
    uuid = db.Column(db.String(36), nullable=False)
    tipo_comprobante = db.Column(db.String(1))  # I=Ingreso, E=Egreso, T=Traslado, N=Nómina, P=Pago
    fecha_emision = db.Column(db.DateTime)
    fecha_timbrado = db.Column(db.DateTime)
    rfc_emisor = db.Column(db.String(13))
    nombre_emisor = db.Column(db.String(300))
    rfc_receptor = db.Column(db.String(13))
    nombre_receptor = db.Column(db.String(300))
    subtotal = db.Column(db.Float, default=0)
    total = db.Column(db.Float, default=0)
    impuestos = db.Column(db.Float, default=0)
    estado = db.Column(db.String(20), default='vigente')  # vigente, cancelado
    uso_cfdi = db.Column(db.String(10))
    metodo_pago = db.Column(db.String(5))
    forma_pago = db.Column(db.String(5))
    serie = db.Column(db.String(10))
    folio = db.Column(db.String(20))
    moneda = db.Column(db.String(5), default='MXN')
    tipo_cambio = db.Column(db.Float, default=1.0)
    xml_content = db.Column(db.Text)
    download_date = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('empresa_id', 'uuid', name='uq_empresa_uuid'),
    )


class DownloadRequest(db.Model):
    __tablename__ = 'download_requests'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=False)
    tipo = db.Column(db.String(20))  # emitidos, recibidos, retenciones
    fecha_inicio = db.Column(db.DateTime)
    fecha_fin = db.Column(db.DateTime)
    estado = db.Column(db.String(20), default='pendiente')  # pendiente, procesando, completado, error
    total_descargados = db.Column(db.Integer, default=0)
    mensaje = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
