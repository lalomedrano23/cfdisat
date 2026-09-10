from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.models import DownloadSchedule, Empresa
from app.scheduler import calcular_proxima_ejecucion, lanzar_ejecucion

programacion_bp = Blueprint('programacion', __name__)

DIAS_SEMANA = ['Lunes', 'Martes', 'Miercoles', 'Jueves', 'Viernes', 'Sabado', 'Domingo']
TIPOS = {
    'todos': 'Todos (emitidos + recibidos)',
    'emitidos': 'Emitidos',
    'recibidos': 'Recibidos',
    'retenciones_emitidas': 'Retenciones emitidas',
    'retenciones_recibidas': 'Retenciones recibidas',
}
PERIODICIDADES = {'diaria': 'Diaria', 'semanal': 'Semanal', 'mensual': 'Mensual'}
HORIZONTES = {
    'mes_anterior': 'Mes anterior (completo)',
    'mes_actual': 'Mes actual (hasta hoy)',
    'periodo_fijo': 'Periodo fijo (fechas especificas)',
}


def _get_schedule_or_403(sched_id):
    sched = db.session.get(DownloadSchedule, sched_id)
    if not sched:
        flash('Programacion no encontrada.', 'error')
        return None
    empresa = db.session.get(Empresa, sched.empresa_id)
    if not empresa or empresa.user_id != current_user.id:
        flash('No tienes acceso a esta programacion.', 'error')
        return None
    return sched


@programacion_bp.route('/programacion')
@login_required
def index():
    empresas = Empresa.query.filter_by(user_id=current_user.id).all()
    schedules = DownloadSchedule.query.join(Empresa).filter(
        Empresa.user_id == current_user.id
    ).order_by(DownloadSchedule.created_at.desc()).all()

    return render_template('programacion/index.html',
                           empresas=empresas,
                           schedules=schedules,
                           tipos=TIPOS,
                           periodicidades=PERIODICIDADES,
                           horizontes=HORIZONTES,
                           dias_semana=DIAS_SEMANA)


@programacion_bp.route('/programacion/crear', methods=['POST'])
@login_required
def crear():
    empresa_id = request.form.get('empresa_id', type=int)
    tipo = request.form.get('tipo', 'todos')
    periodicidad = request.form.get('periodicidad', 'mensual')
    dia_semana = request.form.get('dia_semana', 0, type=int)
    dia_mes = request.form.get('dia_mes', 1, type=int)
    hora = request.form.get('hora', 8, type=int)
    minuto = request.form.get('minuto', 0, type=int)
    horizonte = request.form.get('horizonte', 'mes_anterior')
    incluir_pdf = request.form.get('incluir_pdf') == '1'

    if not empresa_id:
        flash('Seleccione una empresa.', 'error')
        return redirect(url_for('programacion.index'))

    empresa = db.session.get(Empresa, empresa_id)
    if not empresa or empresa.user_id != current_user.id:
        flash('No tienes acceso a esta empresa.', 'error')
        return redirect(url_for('programacion.index'))

    if periodicidad == 'mensual':
        dia_mes = min(max(dia_mes or 1, 1), 28)
        dia_semana = 0
    elif periodicidad == 'semanal':
        dia_semana = min(max(dia_semana or 0, 0), 6)
        dia_mes = 1

    fecha_inicio_fija = request.form.get('fecha_inicio_fija') or None
    fecha_fin_fija = request.form.get('fecha_fin_fija') or None
    if horizonte == 'periodo_fijo':
        from datetime import date as _date
        fecha_inicio_fija = datetime.strptime(fecha_inicio_fija, '%Y-%m-%d').date() if fecha_inicio_fija else None
        fecha_fin_fija = datetime.strptime(fecha_fin_fija, '%Y-%m-%d').date() if fecha_fin_fija else None
        if not fecha_inicio_fija or not fecha_fin_fija:
            flash('Para un periodo fijo debe indicar fecha inicio y fin.', 'error')
            return redirect(url_for('programacion.index'))

    sched = DownloadSchedule(
        empresa_id=empresa.id,
        tipo=tipo,
        periodicidad=periodicidad,
        dia_semana=dia_semana,
        dia_mes=dia_mes,
        hora=hora,
        minuto=minuto,
        horizonte=horizonte,
        fecha_inicio_fija=fecha_inicio_fija,
        fecha_fin_fija=fecha_fin_fija,
        incluir_pdf=incluir_pdf,
        activa=True,
        estado='pendiente',
    )
    sched.proxima_ejecucion = calcular_proxima_ejecucion(sched, datetime.utcnow())
    db.session.add(sched)
    db.session.commit()

    flash('Programacion creada correctamente.', 'success')
    return redirect(url_for('programacion.index'))


@programacion_bp.route('/programacion/<int:sched_id>/toggle', methods=['POST'])
@login_required
def toggle(sched_id):
    sched = _get_schedule_or_403(sched_id)
    if not sched:
        return redirect(url_for('programacion.index'))

    sched.activa = not sched.activa
    if sched.activa and sched.proxima_ejecucion is None:
        sched.proxima_ejecucion = calcular_proxima_ejecucion(sched, datetime.utcnow())
    db.session.commit()

    flash('Programacion {}'.format('activada' if sched.activa else 'desactivada'), 'success')
    return redirect(url_for('programacion.index'))


@programacion_bp.route('/programacion/<int:sched_id>/ejecutar', methods=['POST'])
@login_required
def ejecutar_ahora(sched_id):
    sched = _get_schedule_or_403(sched_id)
    if not sched:
        return redirect(url_for('programacion.index'))

    lanzar_ejecucion(sched, current_app._get_current_object())
    flash('Descarga programada iniciada en segundo plano.', 'success')
    return redirect(url_for('programacion.index'))


@programacion_bp.route('/programacion/<int:sched_id>/eliminar', methods=['POST'])
@login_required
def eliminar(sched_id):
    sched = _get_schedule_or_403(sched_id)
    if not sched:
        return redirect(url_for('programacion.index'))

    db.session.delete(sched)
    db.session.commit()
    flash('Programacion eliminada.', 'success')
    return redirect(url_for('programacion.index'))