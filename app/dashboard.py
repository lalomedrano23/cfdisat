from datetime import datetime, timedelta
from flask import Blueprint, render_template
from flask_login import login_required, current_user
from sqlalchemy import func, extract
from app import db
from app.models import Empresa, CFDI, DownloadRequest

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/')
@login_required
def index():
    empresas = Empresa.query.filter_by(user_id=current_user.id).all()
    empresa_id = None

    stats = {}
    cfdis_recientes = []
    download_requests = []

    if empresas:
        empresa_id = empresas[0].id
        stats = _get_empresa_stats(empresa_id)
        cfdis_recientes = CFDI.query.filter_by(empresa_id=empresa_id)\
            .order_by(CFDI.fecha_emision.desc()).limit(10).all()
        download_requests = DownloadRequest.query.filter_by(empresa_id=empresa_id)\
            .order_by(DownloadRequest.created_at.desc()).limit(5).all()

    return render_template('dashboard/index.html',
                         empresas=empresas,
                         empresa_id=empresa_id,
                         stats=stats,
                         cfdis_recientes=cfdis_recientes,
                         download_requests=download_requests)


@dashboard_bp.route('/empresa/<int:empresa_id>')
@login_required
def ver_empresa(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    if empresa.user_id != current_user.id:
        from flask import flash, redirect, url_for
        flash('No tienes acceso.', 'error')
        return redirect(url_for('dashboard.index'))

    empresas = Empresa.query.filter_by(user_id=current_user.id).all()
    stats = _get_empresa_stats(empresa_id)
    cfdis_recientes = CFDI.query.filter_by(empresa_id=empresa_id)\
        .order_by(CFDI.fecha_emision.desc()).limit(20).all()
    download_requests = DownloadRequest.query.filter_by(empresa_id=empresa_id)\
        .order_by(DownloadRequest.created_at.desc()).limit(10).all()

    return render_template('dashboard/index.html',
                         empresas=empresas,
                         empresa_id=empresa_id,
                         empresaSeleccionada=empresa,
                         stats=stats,
                         cfdis_recientes=cfdis_recientes,
                         download_requests=download_requests)


def _get_empresa_stats(empresa_id):
    total_cfdis = CFDI.query.filter_by(empresa_id=empresa_id).count()
    emitidos = CFDI.query.filter_by(empresa_id=empresa_id, tipo_comprobante='I').count()
    recibidos = CFDI.query.filter_by(empresa_id=empresa_id, tipo_comprobante='R').count()
    egresos = CFDI.query.filter_by(empresa_id=empresa_id, tipo_comprobante='E').count()
    cancelados = CFDI.query.filter_by(empresa_id=empresa_id, estado='cancelado').count()

    total_ingresos = db.session.query(func.sum(CFDI.total))\
        .filter_by(empresa_id=empresa_id, tipo_comprobante='I', estado='vigente')\
        .scalar() or 0
    total_egresos = db.session.query(func.sum(CFDI.total))\
        .filter_by(empresa_id=empresa_id, tipo_comprobante='E', estado='vigente')\
        .scalar() or 0
    total_impuestos = db.session.query(func.sum(CFDI.impuestos))\
        .filter_by(empresa_id=empresa_id, estado='vigente')\
        .scalar() or 0

    hoy = datetime.utcnow()
    mes_actual = hoy.month
    anio_actual = hoy.year

    cfdis_mes = CFDI.query.filter_by(empresa_id=empresa_id)\
        .filter(extract('month', CFDI.fecha_emision) == mes_actual)\
        .filter(extract('year', CFDI.fecha_emision) == anio_actual).count()

    iva_pagado = db.session.query(func.sum(CFDI.impuestos))\
        .filter_by(empresa_id=empresa_id, tipo_comprobante='I', estado='vigente')\
        .filter(extract('month', CFDI.fecha_emision) == mes_actual)\
        .filter(extract('year', CFDI.fecha_emision) == anio_actual)\
        .scalar() or 0

    iva_acreditable = db.session.query(func.sum(CFDI.impuestos))\
        .filter_by(empresa_id=empresa_id, tipo_comprobante='E', estado='vigente')\
        .filter(extract('month', CFDI.fecha_emision) == mes_actual)\
        .filter(extract('year', CFDI.fecha_emision) == anio_actual)\
        .scalar() or 0

    iva_pagar = iva_pagado - iva_acreditable

    return {
        'total_cfdis': total_cfdis,
        'emitidos': emitidos,
        'recibidos': recibidos,
        'egresos': egresos,
        'cancelados': cancelados,
        'total_ingresos': total_ingresos,
        'total_egresos': total_egresos,
        'total_impuestos': total_impuestos,
        'cfdis_mes': cfdis_mes,
        'iva_pagado': iva_pagado,
        'iva_acreditable': iva_acreditable,
        'iva_pagar': iva_pagar,
    }
