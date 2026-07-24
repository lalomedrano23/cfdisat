import io
from datetime import datetime
from flask import Blueprint, render_template, request, send_file, flash, redirect, url_for
from flask_login import login_required, current_user
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from app import db
from app.models import Empresa, CFDI

reportes_bp = Blueprint('reportes', __name__)


@reportes_bp.route('/reportes')
@login_required
def index():
    empresas = Empresa.query.filter_by(user_id=current_user.id).all()
    return render_template('reportes/index.html', empresas=empresas)


@reportes_bp.route('/reportes/iva/<int:empresa_id>')
@login_required
def iva(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    if empresa.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('reportes.index'))

    month = request.args.get('month', datetime.utcnow().month, type=int)
    year = request.args.get('year', datetime.utcnow().year, type=int)

    from sqlalchemy import func, extract

    iva_cobrado = db.session.query(func.sum(CFDI.impuestos))\
        .filter_by(empresa_id=empresa_id, tipo_comprobante='I', estado='vigente')\
        .filter(extract('month', CFDI.fecha_emision) == month)\
        .filter(extract('year', CFDI.fecha_emision) == year)\
        .scalar() or 0

    iva_acreditable = db.session.query(func.sum(CFDI.impuestos))\
        .filter_by(empresa_id=empresa_id, tipo_comprobante='E', estado='vigente')\
        .filter(extract('month', CFDI.fecha_emision) == month)\
        .filter(extract('year', CFDI.fecha_emision) == year)\
        .scalar() or 0

    iva_pagar = iva_cobrado - iva_acreditable

    cfdis_detalle = CFDI.query.filter_by(empresa_id=empresa_id, estado='vigente')\
        .filter(extract('month', CFDI.fecha_emision) == month)\
        .filter(extract('year', CFDI.fecha_emision) == year)\
        .order_by(CFDI.fecha_emision.desc()).all()

    return render_template('reportes/iva.html',
                         empresa=empresa,
                         month=month, year=year,
                         iva_cobrado=iva_cobrado,
                         iva_acreditable=iva_acreditable,
                         iva_pagar=iva_pagar,
                         cfdis_detalle=cfdis_detalle)


@reportes_bp.route('/reportes/exportar-excel/<int:empresa_id>')
@login_required
def exportar_excel(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    if empresa.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('reportes.index'))

    tipo = request.args.get('tipo', '')
    estado = request.args.get('estado', '')

    query = CFDI.query.filter_by(empresa_id=empresa.id)
    if tipo:
        query = query.filter_by(tipo_comprobante=tipo)
    if estado:
        query = query.filter_by(estado=estado)

    cfdis = query.order_by(CFDI.fecha_emision.desc()).all()

    wb = Workbook()
    ws = wb.active
    ws.title = 'CFDIS'

    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill(start_color='1a73e8', end_color='1a73e8', fill_type='solid')
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )

    headers = [
        'UUID', 'Tipo', 'Fecha Emision', 'RFC Emisor', 'Nombre Emisor',
        'RFC Receptor', 'Nombre Receptor', 'Subtotal', 'Impuestos', 'Total',
        'Estado', 'Uso CFDI', 'Moneda'
    ]

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')
        cell.border = thin_border

    tipo_nombres = {'I': 'Ingreso', 'E': 'Egreso', 'T': 'Traslado', 'N': 'Nomina', 'P': 'Pago', 'R': 'Retencion'}

    for row, cf in enumerate(cfdis, 2):
        data = [
            cf.uuid,
            tipo_nombres.get(cf.tipo_comprobante, cf.tipo_comprobante),
            cf.fecha_emision.strftime('%Y-%m-%d %H:%M') if cf.fecha_emision else '',
            cf.rfc_emisor,
            cf.nombre_emisor,
            cf.rfc_receptor,
            cf.nombre_receptor,
            cf.subtotal,
            cf.impuestos,
            cf.total,
            cf.estado.capitalize(),
            cf.uso_cfdi,
            cf.moneda,
        ]
        for col, value in enumerate(data, 1):
            cell = ws.cell(row=row, column=col, value=value)
            cell.border = thin_border
            if col in (8, 9, 10):
                cell.number_format = '#,##0.00'

    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 2, 40)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    filename = f'CFDIS_{empresa.rfc}_{timestamp}.xlsx'

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename
    )
