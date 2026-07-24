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