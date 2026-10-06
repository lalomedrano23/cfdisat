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

# Tiempo maximo (min) que una ejecucion de descarga puede quedar 'procesando'
# antes de considerarse interrumpida (servidor reiniciado / timeout / OOM).
PROCESANDO_MAX_MIN = 45


def _ahora_mx():
    """Hora civil de Mexico (sin informacion de zona para guardarla en BD)."""
    from satcfdi.cfdi import MEXICO_TZ
    return datetime.now(MEXICO_TZ).replace(tzinfo=None)


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

    hoy_mx = date(ahora.year, ahora.month, ahora.day)
    if fin > hoy_mx:
        fin = hoy_mx
    if fin < inicio:
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

    ahora = _ahora_mx()

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

    request_dl = None
    try:
        request_dl = DownloadRequest(
            empresa_id=sched.empresa_id,
            tipo=sched.tipo,
            fecha_inicio=datetime.strptime(fecha_ini, '%Y-%m-%d'),
            fecha_fin=datetime.strptime(fecha_fin, '%Y-%m-%d'),
            estado='procesando'
        )
        db.session.add(request_dl)
        db.session.commit()
    except Exception:
        logger.exception('[PROGRAMACION] No se pudo crear el DownloadRequest, se marca error')
        db.session.rollback()
        sched.estado = 'error'
        sched.mensaje = 'No se pudo iniciar la descarga (error interno). Revisa el log.'
        db.session.commit()
        return

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
                s.proxima_ejecucion = calcular_proxima_ejecucion(s, _ahora_mx())
                db.session.commit()
        except Exception:
            logger.exception('[PROGRAMACION] Error interno al ejecutar tarea')

    t = threading.Thread(target=_run, daemon=True, name=f'sched-{sched_id}')
    t.start()


def _recuperar_procesos_stuck(ahora):
    """Marca como 'error' las descargas que quedaron 'procesando' y ya no estan vivas.

    Si el proceso del servidor se reinicia (Render recicla el worker, OOM, timeout),
    el hilo de descarga muere sin poder actualizar el estado; sin esto, la programacion
    quedaria 'procesando' para siempre.
    """
    from app.models import DownloadRequest as ReqModel
    from app.models import DownloadSchedule as SchedModel

    limite = ahora - timedelta(minutes=PROCESANDO_MAX_MIN)

    schedules = SchedModel.query.filter_by(estado='procesando')\
        .filter(SchedModel.ultima_ejecucion.isnot(None),
                SchedModel.ultima_ejecucion < limite).all()
    for s in schedules:
        s.estado = 'error'
        s.mensaje = ('La descarga se interrumpio (el servidor se reinicio o la tarea '
                     'excedio el tiempo maximo). Lanzala de nuevo desde Programacion.')
        s.proxima_ejecucion = calcular_proxima_ejecucion(s, ahora)

    limite_utc = datetime.utcnow() - timedelta(minutes=PROCESANDO_MAX_MIN)
    requests = ReqModel.query.filter_by(estado='procesando')\
        .filter(ReqModel.created_at < limite_utc).all()
    for r in requests:
        r.estado = 'error'
        r.mensaje = 'La descarga se interrumpio (servidor reiniciado o timeout).'
        r.completed_at = ahora

    if schedules or requests:
        db.session.commit()
        for s in schedules:
            logger.warning('[PROGRAMACION] Ejecucion %s recuperada de "procesando" a "error"', s.id)


def _revisar_programaciones(app):
    from app.models import DownloadSchedule

    ahora = _ahora_mx()
    _recuperar_procesos_stuck(ahora)

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