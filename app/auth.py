from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from app import db
from app.models import User

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        nombre = request.form.get('nombre', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        if not email or not nombre or not password:
            flash('Todos los campos son obligatorios.', 'error')
            return render_template('register.html')

        if password != confirm:
            flash('Las contrasenas no coinciden.', 'error')
            return render_template('register.html')

        if len(password) < 6:
            flash('La contrasena debe tener al menos 6 caracteres.', 'error')
            return render_template('register.html')

        if User.query.filter_by(email=email).first():
            flash('Este correo ya esta registrado.', 'error')
            return render_template('register.html')

        user = User(email=email, nombre=nombre)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash('Cuenta creada exitosamente.', 'success')
        return redirect(url_for('fiel.configurar_fiel'))

    return render_template('register.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            flash('Bienvenido.', 'success')
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard.index'))
        flash('Correo o contrasena incorrectos.', 'error')
    return render_template('login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sesion cerrada.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/cambiar-password', methods=['GET', 'POST'])
@login_required
def cambiar_password():
    if request.method == 'POST':
        actual = request.form.get('password_actual', '')
        nueva = request.form.get('password_nueva', '')
        confirmar = request.form.get('password_confirmar', '')

        if not current_user.check_password(actual):
            flash('La contrasena actual es incorrecta.', 'error')
            return render_template('cambiar_password.html')

        if len(nueva) < 6:
            flash('La nueva contrasena debe tener al menos 6 caracteres.', 'error')
            return render_template('cambiar_password.html')

        if nueva != confirmar:
            flash('Las contrasenas no coinciden.', 'error')
            return render_template('cambiar_password.html')

        current_user.set_password(nueva)
        db.session.commit()
        flash('Contrasena actualizada.', 'success')
        return redirect(url_for('dashboard.index'))

    return render_template('cambiar_password.html')