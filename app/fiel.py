import os
import shutil
from flask import Blueprint, render_template, redirect, url_for, flash, current_app, request
from flask_login import login_required, current_user
from cryptography.fernet import Fernet
from app import db
from app.models import Empresa, FielCredentials

fiel_bp = Blueprint('fiel', __name__)

ENCRYPTION_KEY = Fernet.generate_key()
cipher = Fernet(ENCRYPTION_KEY)


def encrypt_password(password):
    return cipher.encrypt(password.encode()).decode()


def decrypt_password(encrypted):
    return cipher.decrypt(encrypted.encode()).decode()


@fiel_bp.route('/configurar-fiel', methods=['GET', 'POST'])
@login_required
def configurar_fiel():
    empresas = Empresa.query.filter_by(user_id=current_user.id).all()

    if request.method == 'POST':
        rfc = request.form.get('rfc', '').strip().upper()
        razon_social = request.form.get('razon_social', '').strip()
        cer_file = request.files.get('cer_file')
        key_file = request.files.get('key_file')
        password = request.form.get('password', '')

        if not rfc or not razon_social or not cer_file or not key_file or not password:
            flash('Todos los campos son obligatorios.', 'error')
            return render_template('fiel/configurar.html', empresas=empresas)

        if not cer_file.filename.endswith('.cer'):
            flash('El archivo de certificado debe tener extension .cer', 'error')
            return render_template('fiel/configurar.html', empresas=empresas)

        if not key_file.filename.endswith('.key'):
            flash('El archivo de llave privada debe tener extension .key', 'error')
            return render_template('fiel/configurar.html', empresas=empresas)

        empresa = Empresa(
            user_id=current_user.id,
            rfc=rfc,
            razon_social=razon_social
        )
        db.session.add(empresa)
        db.session.flush()

        fiel_dir = os.path.join(current_app.config['UPLOAD_FOLDER_FIEL'], rfc)
        os.makedirs(fiel_dir, exist_ok=True)

        cer_path = os.path.join(fiel_dir, f'{rfc}.cer')
        key_path = os.path.join(fiel_dir, f'{rfc}.key')
        cer_file.save(cer_path)
        key_file.save(key_path)

        fiel = FielCredentials(
            empresa_id=empresa.id,
            cer_filename=f'{rfc}.cer',
            key_filename=f'{rfc}.key',
            password_encrypted=encrypt_password(password),
            rfc=rfc,
            nombre=razon_social
        )
        db.session.add(fiel)
        db.session.commit()

        flash('Credenciales FIEL configuradas exitosamente.', 'success')
        return redirect(url_for('dashboard.index'))

    return render_template('fiel/configurar.html', empresas=empresas)


@fiel_bp.route('/empresa/<int:empresa_id>/editar', methods=['GET', 'POST'])
@login_required
def editar_empresa(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    if empresa.user_id != current_user.id:
        flash('No tienes acceso a esta empresa.', 'error')
        return redirect(url_for('fiel.configurar_fiel'))

    if request.method == 'POST':
        empresa.razon_social = request.form.get('razon_social', '').strip()

        cer_file = request.files.get('cer_file')
        key_file = request.files.get('key_file')
        password = request.form.get('password', '')

        fiel = FielCredentials.query.filter_by(empresa_id=empresa.id).first()

        if cer_file and cer_file.filename:
            fiel_dir = os.path.join(current_app.config['UPLOAD_FOLDER_FIEL'], empresa.rfc)
            cer_path = os.path.join(fiel_dir, fiel.cer_filename)
            cer_file.save(cer_path)

        if key_file and key_file.filename:
            fiel_dir = os.path.join(current_app.config['UPLOAD_FOLDER_FIEL'], empresa.rfc)
            key_path = os.path.join(fiel_dir, fiel.key_filename)
            key_file.save(key_path)

        if password:
            fiel.password_encrypted = encrypt_password(password)

        db.session.commit()
        flash('Empresa actualizada.', 'success')
        return redirect(url_for('fiel.configurar_fiel'))

    return render_template('fiel/editar.html', empresa=empresa)


@fiel_bp.route('/empresa/<int:empresa_id>/eliminar', methods=['POST'])
@login_required
def eliminar_empresa(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    if empresa.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('fiel.configurar_fiel'))

    fiel = FielCredentials.query.filter_by(empresa_id=empresa.id).first()
    if fiel:
        fiel_dir = os.path.join(current_app.config['UPLOAD_FOLDER_FIEL'], empresa.rfc)
        if os.path.exists(fiel_dir):
            shutil.rmtree(fiel_dir)
        db.session.delete(fiel)

    CFDI = __import__('app.models', fromlist=['CFDI']).CFDI
    CFDI.query.filter_by(empresa_id=empresa.id).delete()
    db.session.delete(empresa)
    db.session.commit()
    flash('Empresa eliminada.', 'info')
    return redirect(url_for('fiel.configurar_fiel'))
