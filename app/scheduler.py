import calendar
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta

from app import db

logger = logging.getLogger(__name__)

_started = False
_lock = threading.Lock()


def _dias_en_mes(anio, mes):
    return calendar.monthrange(anio, mes)[1]


def calcular_periodo(sched, ahora):
    from app.models import DownloadSchedule

    if not isinstance(sched, DownloadSchedule):
        return None, None

    if sched.horizonte == 'mes_anterior':
        if ahora.month == 1:
            anio, mes = ahora.year - 1, 12
        else:
            anio, mes = ahora.year, ahora.month - 1
        inicio = date(anio, mes, 1)
        fin = date(anio, mes, _dias_en_mes(anio, mes))
    elif sched.horizonte == 'mes_actual':
        inicio = date(ahora.year, ahora.month, 1)
        fin = date(ahora.year, ahora.month, ahora.day)
    else:
        inicio = sched.fecha_inicio_fija
        fin = sched.fecha_fin_fija

    if not inicio or not fin:
        return None, None
    return inicio.isoformat(), fin.isoformat()


def calcular_proxima_ejecucion(sched, ahora):
    hora = sched.hora or 0
    minuto = sched.minuto or 0

    if sched.periodicidad == 'diaria':
        nxt = ahora + timedelta(days=1)
        return nxt.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    elif sched.periodicidad == 'semanal':
        objetivo = sched.dia_semana or 0
        dias = (objetivo - ahora.weekday()) % 7
        if dias == 0:
            dias = 7
        nxt = ahora + timedelta(days=dias)
        return nxt.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    else:  # mensual
        anio = ahora.year
        mes = ahora.month + 1
        if mes == 13:
            mes = 1
            anio += 1
        dia = min(sched.dia_mes or 1, _dias_en_mes(anio, mes))
        return datetime(anio, mes, dia, hora, minuto, 0)


def lanzar_ejecucion(sched, app, programada=True):
    from app.models import DownloadRequest

    ahora = datetime.utcnow()

    if sched.estado == 'procesando':
        logger.info(f'[PROGRAMACION] Solicitud {sched.id} ya en proceso, se omite.')
        return

    fecha_ini, fecha_fin = calcular_periodo(sched, ahora)
    if not fecha_ini or not fecha_fin:
        sched.estado = 'error'
        sched.mensaje = 'No se pudo calcular el periodo (revisa las fechas fijas).'
        db.session.commit()
        return

    sched.estado = 'procesando'
    sched.ultima_ejecucion = ahora
    db.session.commit()

    request_dl = DownloadRequest(
        empresa_id=sched.empresa_id,
        tipo=sched.tipo,
        fecha_inicio=datetime.strptime(fecha_ini, '%Y-%m-%d'),
        fecha_fin=datetime.strptime(fecha_fin, '%Y-%m-%d'),
        estado='procesando'
    )
    db.session.add(request_dl)
    db.session.commit()

    sched_id = sched.id
    req_id = request_dl.id

    def _run():
        try:
            err = None
            try:
                with app.app_context():
                    from app.sat import _ejecutar_descarga_sat
                    _ejecutar_descarga_sat(req_id, sched.empresa_id, sched.tipo, fecha_ini, fecha_fin, sched.incluir_pdf)
            except Exception as e:
                err = str(e)
                logger.exception('[PROGRAMACION] Error en descarga programada')

            with app.app_context():
                from app.models import DownloadSchedule as SchedModel
                from app.models import DownloadRequest as ReqModel
                s = db.session.get(SchedModel, sched_id)
                if s is None:
                    return
                req = db.session.get(ReqModel, req_id)
                total = req.total_descargados if req else 0
                s.estado = 'ok' if not err else 'error'
                s.mensaje = err if err else (f'Descargados {total} CFDIs.')
                s.proxima_ejecucion = calcular_proxima_ejecucion(s, datetime.utcnow())
                db.session.commit()
        except Exception:
            logger.exception('[PROGRAMACION] Error interno al ejecutar tarea')

    t = threading.Thread(target=_run, daemon=True, name=f'sched-{sched_id}')
    t.start()


def _revisar_programaciones(app):
    from app.models import DownloadSchedule

    ahora = datetime.utcnow()
    vencidas = DownloadSchedule.query.filter(
        DownloadSchedule.activa.is_(True),
        DownloadSchedule.proxima_ejecucion.isnot(None),
        DownloadSchedule.proxima_ejecucion <= ahora,
    ).all()

    for sched in vencidas:
        try:
            lanzar_ejecucion(sched, app, programada=True)
        except Exception:
            logger.exception('[PROGRAMACION] Error al lanzar tarea programada')


def start_scheduler(app):
    global _started
    with _lock:
        if _started:
            return
        _started = True

    def _loop():
        with app.app_context():
            logger.info('[PROGRAMACION] Scheduler iniciado (revisa cada 60s).')
        while True:
            try:
                with app.app_context():
                    _revisar_programaciones(app)
            except Exception:
                logger.exception('[PROGRAMACION] Error en ciclo del scheduler')
            time.sleep(60)

    t = threading.Thread(target=_loop, daemon=True, name='scheduler')
    t.start()


def scheduler_debe_iniciar(app):
    if os.environ.get('DISABLE_SCHEDULER'):
        return False
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        return True
    return not app.debug