from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, Response, url_for
from flask_login import current_user, login_required
from sqlalchemy import case, extract, func
from sqlalchemy.sql import desc

from app import db
from app.models import CFDI, Empresa
from app.sat import build_cfdis_zip

cfdis_bp = Blueprint('cfdis', __name__)

MESES = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
         'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']

PER_PAGE = 50


def _get_empresa_or_403(empresa_id):
    empresa = db.session.get(Empresa, empresa_id)
    if not empresa or empresa.user_id != current_user.id:
        flash('No tienes acceso a esta empresa.', 'error')
        return None
    return empresa


@cfdis_bp.route('/cfdis')
@login_required
def indice():
    empresas = Empresa.query.filter_by(user_id=current_user.id).all()
    empresa_id = request.args.get('empresa_id', type=int) or (empresas[0].id if empresas else None)

    meses_por_anio = {}
    empresa = None
    total_rows = 0

    if empresa_id:
        empresa = _get_empresa_or_403(empresa_id)
        if empresa:
            rows = db.session.query(
                extract('year', CFDI.fecha_emision).label('anio'),
                extract('month', CFDI.fecha_emision).label('mes'),
                func.count(CFDI.id).label('total'),
                func.sum(case((CFDI.rfc_emisor == empresa.rfc, 1), else_=0)).label('emitidos'),
                func.sum(case((CFDI.rfc_receptor == empresa.rfc, 1), else_=0)).label('recibidos'),
                func.sum(case((CFDI.estado == 'vigente', CFDI.total), else_=0)).label('total_vigente'),
            ).filter(
                CFDI.empresa_id == empresa.id
            ).group_by('anio', 'mes').order_by(desc('anio'), desc('mes')).all()

            for r in rows:
                anio = int(r.anio or 0)
                mes = int(r.mes or 0)
                if anio and mes:
                    meses_por_anio.setdefault(anio, []).append({
                        'mes': mes,
                        'nombre': MESES[mes - 1],
                        'total': int(r.total or 0),
                        'emitidos': int(r.emitidos or 0),
                        'recibidos': int(r.recibidos or 0),
                        'total_vigente': float(r.total_vigente or 0),
                    })
            total_rows = sum(m['total'] for anio_rows in meses_por_anio.values() for m in anio_rows)

    anios = sorted(meses_por_anio.keys(), reverse=True)
    return render_template('cfdis/indice.html',
                           empresas=empresas,
                           empresa_id=empresa_id,
                           empresa=empresa,
                           meses_por_anio=meses_por_anio,
                           anios=anios,
                           total_rows=total_rows,
                           mes_actual=datetime.utcnow().month)


@cfdis_bp.route('/cfdis/mes/<int:empresa_id>/<int:anio>/<int:mes>')
@login_required
def mes_detalle(empresa_id, anio, mes):
    empresa = _get_empresa_or_403(empresa_id)
    if not empresa:
        return redirect(url_for('cfdis.indice'))

    if not (1 <= mes <= 12):
        flash('Mes invalido.', 'error')
        return redirect(url_for('cfdis.indice', empresa_id=empresa_id))

    tipo = request.args.get('tipo', 'todos')
    en_estado = request.args.get('estado', '')
    page = request.args.get('page', 1, type=int)

    query = CFDI.query.filter(
        CFDI.empresa_id == empresa.id,
        extract('year', CFDI.fecha_emision) == anio,
        extract('month', CFDI.fecha_emision) == mes,
    )
    if tipo == 'emitidos':
        query = query.filter(CFDI.rfc_emisor == empresa.rfc)
    elif tipo == 'recibidos':
        query = query.filter(CFDI.rfc_receptor == empresa.rfc)
    if en_estado:
        query = query.filter(CFDI.estado == en_estado)

    query = query.order_by(CFDI.fecha_emision.desc())

    total_query = query.with_entities(func.count(CFDI.id))
    total_count = total_query.scalar() or 0
    total_monto = query.with_entities(func.coalesce(func.sum(CFDI.total), 0)).scalar() or 0

    pagination = query.paginate(page=page, per_page=PER_PAGE, error_out=False)
    cfdis_pagina = pagination.items

    def _contar(tipo_filtro):
        q = CFDI.query.filter(
            CFDI.empresa_id == empresa.id,
            extract('year', CFDI.fecha_emision) == anio,
            extract('month', CFDI.fecha_emision) == mes,
        )
        if tipo_filtro == 'emitidos':
            q = q.filter(CFDI.rfc_emisor == empresa.rfc)
        elif tipo_filtro == 'recibidos':
            q = q.filter(CFDI.rfc_receptor == empresa.rfc)
        return q.count()

    conteos = {'todos': _contar('todos'), 'emitidos': _contar('emitidos'), 'recibidos': _contar('recibidos')}

    return render_template('cfdis/mes.html',
                           empresa=empresa,
                           anio=anio,
                           mes=mes,
                           nombre_mes=MESES[mes - 1],
                           tipo=tipo,
                           en_estado=en_estado,
                           cfdis=cfdis_pagina,
                           pagination=pagination,
                           per_page=PER_PAGE,
                           total_count=total_count,
                           total_monto=total_monto,
                           conteos=conteos)


@cfdis_bp.route('/cfdis/exportar', methods=['POST'])
@login_required
def exportar():
    empresa_id = request.form.get('empresa_id', type=int)
    anio = request.form.get('anio', type=int)
    mes = request.form.get('mes', type=int)
    tipo = request.form.get('tipo', 'todos')
    formato = request.form.get('formato', 'xml')
    accion = request.form.get('accion', 'seleccion')
    exportar_mes = (accion == 'mes') or (request.form.get('exportar_mes') == '1')
    cfdis_ids = request.form.getlist('cfdis')

    if not empresa_id:
        flash('Seleccione una empresa.', 'error')
        return redirect(url_for('cfdis.indice'))

    empresa = _get_empresa_or_403(empresa_id)
    if not empresa:
        return redirect(url_for('cfdis.indice'))

    redirect_url = url_for('cfdis.mes_detalle', empresa_id=empresa_id, anio=anio or datetime.utcnow().year, mes=mes or datetime.utcnow().month, tipo=tipo)

    query = CFDI.query.filter(CFDI.empresa_id == empresa.id)
    if exportar_mes:
        if not anio or not mes:
            flash('Indique mes y anio para exportar todo el mes.', 'error')
            return redirect(redirect_url)
        query = query.filter(
            extract('year', CFDI.fecha_emision) == anio,
            extract('month', CFDI.fecha_emision) == mes,
        )
        if tipo == 'emitidos':
            query = query.filter(CFDI.rfc_emisor == empresa.rfc)
        elif tipo == 'recibidos':
            query = query.filter(CFDI.rfc_receptor == empresa.rfc)
        cfdis = query.order_by(CFDI.fecha_emision.asc()).all()
    else:
        if not cfdis_ids:
            flash('Seleccione al menos un CFDI.', 'error')
            return redirect(redirect_url)
        cfdis = query.filter(CFDI.id.in_([int(x) for x in cfdis_ids])).all()

    if not cfdis:
        flash('No hay CFDIs para exportar.', 'error')
        return redirect(redirect_url)

    buf = None
    added = 0
    err = None
    try:
        buf, added = build_cfdis_zip(cfdis, formato)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        err = str(e)

    if err or added == 0:
        flash(err or 'No hay archivos disponibles para los CFDIs seleccionados.', 'error')
        return redirect(redirect_url)

    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    tipo_label = {'xml': 'XML', 'pdf': 'PDF', 'ambos': 'XML_PDF'}.get(formato, 'XML')
    prefijo = f'{anio:04d}-{mes:02d}' if anio and mes else 'CFDIS'
    filename = f'{prefijo}_{empresa.rfc}_{tipo_label}_{ts}.zip'

    return Response(
        buf.getvalue(),
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )