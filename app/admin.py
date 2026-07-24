from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app import db
from app.models import User, Empresa, FielCredentials, CFDI, DownloadRequest
import shutil
import os

admin_bp = Blueprint('admin', __name__)


def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('No tienes permisos de administrador.', 'error')
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/admin')
@login_required
@admin_required
def index():
    users = User.query.all()
    total_empresas = Empresa.query.count()
    total_cfdis = CFDI.query.count()
    return render_template('admin/index.html',
                         users=users,
                         total_empresas=total_empresas,
                         total_cfdis=total_cfdis)


@admin_bp.route('/admin/usuario/<int:user_id>/eliminar', methods=['POST'])
@login_required
@admin_required
def eliminar_usuario(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('No puedes eliminarte a ti mismo.', 'error')
        return redirect(url_for('admin.index'))

    empresas = Empresa.query.filter_by(user_id=user.id).all()
    fiel_dir_base = current_app.config.get('UPLOAD_FOLDER_FIEL', '')
    for emp in empresas:
        fiel = FielCredentials.query.filter_by(empresa_id=emp.id).first()
        if fiel and fiel_dir_base:
            emp_dir = os.path.join(fiel_dir_base, emp.rfc)
            if os.path.exists(emp_dir):
                shutil.rmtree(emp_dir, ignore_errors=True)
        CFDI.query.filter_by(empresa_id=emp.id).delete()
        DownloadRequest.query.filter_by(empresa_id=emp.id).delete()
        if fiel:
            db.session.delete(fiel)
        db.session.delete(emp)

    db.session.delete(user)
    db.session.commit()
    flash(f'Usuario {user.email} eliminado con todas sus empresas y datos.', 'info')
    return redirect(url_for('admin.index'))


@admin_bp.route('/admin/usuario/<int:user_id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_usuario(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('No puedes desactivarte a ti mismo.', 'error')
        return redirect(url_for('admin.index'))

    user.activo = not user.activo
    db.session.commit()
    estado = 'activado' if user.activo else 'desactivado'
    flash(f'Usuario {user.email} {estado}.', 'success')
    return redirect(url_for('admin.index'))


@admin_bp.route('/admin/usuario/<int:user_id>/hacer-admin', methods=['POST'])
@login_required
@admin_required
def hacer_admin(user_id):
    user = User.query.get_or_404(user_id)
    user.is_admin = not user.is_admin
    db.session.commit()
    rol = 'administrador' if user.is_admin else 'usuario normal'
    flash(f'{user.email} ahora es {rol}.', 'success')
    return redirect(url_for('admin.index'))


from flask import current_app
