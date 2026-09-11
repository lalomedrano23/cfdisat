"""Modulo Facturacion: Validacion de RFC Acreedor y Creacion + Timbrado de CFDIs 4.0."""

import json
import uuid as uuidmod
from datetime import datetime

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.models import Empresa, Factura, FielCredentials, ValidacionRFC
from app.sat import _create_signer
from app.validacion_rfc import REGIMENES, serializar, validar_rfc

facturacion_bp = Blueprint('facturacion', __name__)

USO_CFDI = {
    'G01': 'Adquisicion de mercancias',
    'G02': 'Devoluciones, descuentos o bonificaciones',
    'G03': 'Gastos en general',
    'I01': 'Construcciones',
    'I02': 'Mobiliario y equipo de oficina por inversiones',
    'I08': 'Otras maquinarias y equipo',
    'D01': 'Honorarios medicos, dentales y gastos hospitalarios',
    'D02': 'Gastos medicos por incapacidad o discapacidad',
    'D03': 'Gastos funerales',
    'D04': 'Donativos',
    'D05': 'Intereses reales efectivamente pagados por creditos hipotecarios',
    'D06': 'Aportaciones voluntarias al SAR',
    'D07': 'Primas por seguros de gastos medicos',
    'D08': 'Gastos de transportacion escolar obligatoria',
    'D09': 'Depositos en cuentas para el ahorro',
    'D10': 'Pagos por servicios educativos',
    'P01': 'Por definir',
    'S01': 'Sin efectos fiscales',
    'CP01': 'Pagos',
    'CN01': 'Nomina',
}

FORMA_PAGO = {
    '01': 'Efectivo',
    '02': 'Cheque nominativo',
    '03': 'Transferencia electronica de fondos',
    '04': 'Tarjeta de credito',
    '05': 'Monedero electronico',
    '15': 'Condonaciones',
    '17': 'Compensacion',
    '23': 'Novacion',
    '99': 'Por definir',
}

METODO_PAGO = {
    'PUE': 'Pago en una sola exhibicion',
    'PPD': 'Pago en parcialidades o diferido',
}


def _empresas_usuario():
    return Empresa.query.filter_by(user_id=current_user.id).all()


def _get_empresa(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    if empresa.user_id != current_user.id:
        raise PermissionError('No tienes acceso a esta empresa.')
    return empresa


def _sugerir_folio(empresa_id, serie):
    serie = (serie or '').upper()
    ultimo = Factura.query.filter_by(empresa_id=empresa_id, serie=serie)\
        .order_by(Factura.id.desc()).first()
    if ultimo and ultimo.folio and str(ultimo.folio).isdigit():
        return str(int(ultimo.folio) + 1)
    return '1'


@facturacion_bp.route('/facturacion')
@login_required
def index():
    facturas = Factura.query.filter_by(user_id=current_user.id)\
        .order_by(Factura.created_at.desc()).limit(10).all()
    validaciones = ValidacionRFC.query.filter_by(user_id=current_user.id)\
        .order_by(ValidacionRFC.consultado_en.desc()).limit(10).all()
    return render_template('facturacion/index.html',
                           facturas=facturas,
                           validaciones=validaciones)


# --------------------------------------------------------------------------- #
# Validacion RFC / CURP Acreedor
# --------------------------------------------------------------------------- #

@facturacion_bp.route('/facturacion/validacion', methods=['GET'])
@login_required
def validacion():
    registros = ValidacionRFC.query.filter_by(user_id=current_user.id)\
        .order_by(ValidacionRFC.consultado_en.desc()).all()
    empresas = _empresas_usuario()
    return render_template('facturacion/validacion.html', registros=registros, empresas=empresas)


@facturacion_bp.route('/facturacion/validacion/validar', methods=['POST'])
@login_required
def validar_registro():
    rfc = request.form.get('rfc', '').strip().upper()
    curp = request.form.get('curp', '').strip().upper()
    id_cif = request.form.get('id_cif', '').strip()
    empresa_id = request.form.get('empresa_id', type=int)

    if not rfc and not curp:
        flash('Ingresa un RFC o una CURP para validar.', 'error')
        return redirect(url_for('facturacion.validacion'))

    if empresa_id:
        try:
            _get_empresa(empresa_id)
        except PermissionError:
            empresa_id = None

    resultado = validar_rfc(rfc=rfc or None, curp=curp or None, id_cif=id_cif or None)

    registro = ValidacionRFC(
        user_id=current_user.id,
        empresa_id=empresa_id or None,
        rfc=resultado['rfc'] or (curp or ''),
        curp=resultado['curp'],
        persona_moral=resultado['persona_moral'],
        razon_social=resultado['razon_social'],
        nombre=resultado['nombre'],
        codigo_postal_fiscal=(resultado['codigo_postal_fiscal'] or '')[:10],
        regimen_fiscal=resultado['regimen_fiscal'],
        regimen_fiscal_desc=resultado['regimen_fiscal_desc'],
        lista_69=(resultado['lista_69'] or '')[:40],
        nss=(resultado['nss'] or '')[:20],
        fuente=resultado['fuente'],
        resultado_completo=serializar(resultado),
        estado=resultado['estado'],
        mensaje=resultado['mensaje'],
        consultado_en=datetime.utcnow(),
    )
    db.session.add(registro)
    db.session.commit()

    if resultado['estado'] == 'error':
        flash(f'Error en la validacion: {resultado["mensaje"]}', 'error')
    elif resultado['lista_69']:
        flash(f'RFC {resultado["rfc"]}: estatus Art. 69-B = {resultado["lista_69"]}', 'info')
    else:
        flash(resultado['mensaje'], 'success')

    return redirect(url_for('facturacion.validacion'))


@facturacion_bp.route('/facturacion/validacion/eliminar/<int:reg_id>', methods=['POST'])
@login_required
def eliminar_validacion(reg_id):
    reg = ValidacionRFC.query.get_or_404(reg_id)
    if reg.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('facturacion.validacion'))
    db.session.delete(reg)
    db.session.commit()
    flash('Registro de validacion eliminado.', 'success')
    return redirect(url_for('facturacion.validacion'))


# --------------------------------------------------------------------------- #
# Crear factura CFDI 4.0
# --------------------------------------------------------------------------- #

@facturacion_bp.route('/facturacion/crear', methods=['GET', 'POST'])
@login_required
def crear():
    empresas = _empresas_usuario()
    validaciones = ValidacionRFC.query.filter_by(user_id=current_user.id)\
        .order_by(ValidacionRFC.consultado_en.desc()).all()

    if request.method == 'POST':
        return _procesar_creacion(empresas)

    form = {
        'empresa_id': request.args.get('empresa_id', type=int),
        'receptor_id': request.args.get('receptor_id', type=int),
    }
    return render_template('facturacion/crear_factura.html',
                           empresas=empresas,
                           validaciones=validaciones,
                           regimenes=REGIMENES,
                           uso_cfdi=USO_CFDI,
                           forma_pago=FORMA_PAGO,
                           metodo_pago=METODO_PAGO,
                           form=form)


def _procesar_creacion(empresas):
    empresa_id = request.form.get('empresa_id', type=int)
    receptor_id = request.form.get('receptor_id', type=int)
    rfc_receptor = request.form.get('rfc_receptor', '').strip().upper()
    nombre_receptor = request.form.get('nombre_receptor', '').strip().upper()
    cp_receptor = request.form.get('cp_receptor', '').strip()
    regimen_receptor = request.form.get('regimen_receptor', '')
    uso_cfdi = request.form.get('uso_cfdi', 'G03')
    regimen_emisor = request.form.get('regimen_emisor', '601')
    lugar_expedicion = request.form.get('lugar_expedicion', '').strip()
    serie = request.form.get('serie', 'A').strip().upper()
    folio = request.form.get('folio', '').strip()
    forma_pago = request.form.get('forma_pago', '99')
    metodo_pago = request.form.get('metodo_pago', 'PUE')
    moneda = request.form.get('moneda', 'MXN').strip().upper()

    descripcion = request.form.get('descripcion', '').strip()
    cantidad = request.form.get('cantidad', '1').strip()
    valor_unitario = request.form.get('valor_unitario', '0').strip()
    aplica_iva = request.form.get('aplica_iva') == 'on'
    timbrar_ahora = request.form.get('timbrar_ahora') == 'on'

    if not rfc_receptor or not nombre_receptor or not cp_receptor:
        flash('Completa los datos del receptor (RFC, Razon Social y CP fiscal).', 'error')
        return redirect(url_for('facturacion.crear'))

    if not regimen_emisor:
        flash('Indica el regimen fiscal del emisor.', 'error')
        return redirect(url_for('facturacion.crear'))

    try:
        empresa = _get_empresa(empresa_id)
    except PermissionError:
        flash('No tienes acceso a esta empresa.', 'error')
        return redirect(url_for('facturacion.crear'))
    signer, error = _create_signer(empresa)
    if error:
        flash(f'No se pudo preparar la FIEL del emisor: {error}', 'error')
        return redirect(url_for('facturacion.crear'))

    from app.facturacion_cfdi import construir_comprobante, redondear
    from satcfdi.cfdi import MEXICO_TZ

    if not lugar_expedicion:
        lugar_expedicion = cp_receptor

    try:
        cant = redondear(cantidad if str(cantidad).isdigit() or _es_decimal(cantidad) else 1)
        val_unit = redondear(valor_unitario)
        subtotal = redondear(cant * val_unit)
    except Exception:
        flash('Revisa la cantidad y el valor unitario del concepto.', 'error')
        return redirect(url_for('facturacion.crear'))

    iva = redondear(subtotal * redondear('0.16')) if aplica_iva else redondear('0')
    total = redondear(subtotal + iva)

    fiel = FielCredentials.query.filter_by(empresa_id=empresa.id).first()
    emisor_nombre = (fiel.nombre if fiel and fiel.nombre else empresa.razon_social).strip().upper() or 'EMISOR'
    emisor_rfc = (fiel.rfc if fiel and fiel.rfc else empresa.rfc).strip().upper()

    conceptos = [{
        'descripcion': descripcion or 'Servicio',
        'cantidad': cant,
        'valor_unitario': val_unit,
        'iva_importe': iva,
        'aplica_iva': aplica_iva,
        'isr_retenido': None,
        'iva_retenido': None,
    }]

    datos = {
        'emisor_rfc': emisor_rfc,
        'emisor_nombre': emisor_nombre,
        'emisor_regimen_fiscal': regimen_emisor,
        'lugar_expedicion': lugar_expedicion or cp_receptor,
        'receptor_rfc': rfc_receptor,
        'receptor_nombre': nombre_receptor,
        'receptor_cp': cp_receptor,
        'receptor_regimen_fiscal': regimen_receptor or '616',
        'uso_cfdi': uso_cfdi,
        'conceptos': conceptos,
        'forma_pago': forma_pago,
        'metodo_pago': metodo_pago,
        'moneda': moneda,
        'tipo_cambio': '1' if moneda == 'MXN' else request.form.get('tipo_cambio', '1'),
        'serie': serie,
        'folio': folio or _sugerir_folio(empresa.id, serie),
        'fecha': datetime.now(MEXICO_TZ),
    }

    factura = Factura(
        user_id=current_user.id,
        empresa_id=empresa.id,
        serie=serie or 'A',
        folio=datos['folio'],
        fecha_emision=datetime.now(MEXICO_TZ),
        rfc_receptor=rfc_receptor,
        nombre_receptor=nombre_receptor,
        uso_cfdi=uso_cfdi,
        regimen_fiscal_receptor=regimen_receptor or '616',
        codigo_postal_receptor=cp_receptor,
        lugar_expedicion=lugar_expedicion or cp_receptor,
        forma_pago=forma_pago,
        metodo_pago=metodo_pago,
        moneda=moneda,
        subtotal=float(subtotal),
        iva=float(iva),
        total=float(total),
        concepto_json=json.dumps(conceptos, ensure_ascii=False, default=str),
        estado='borrador',
    )

    try:
        cfdi, xml = construir_comprobante(datos, signer)
        factura.xml_content = xml.decode('utf-8')
    except Exception as e:
        current_app.logger.error(f'[FACTURACION] Error al construir CFDI: {e}', exc_info=True)
        flash(f'No se pudo construir el CFDI: {e}', 'error')
        return redirect(url_for('facturacion.crear'))

    db.session.add(factura)
    db.session.commit()

    if timbrar_ahora:
        _timbrar_factura(factura.id)

    flash(f'Factura {datos["folio"]} creada en estado {factura.estado}.', 'success')
    return redirect(url_for('facturacion.detalle', factura_id=factura.id))


def _es_decimal(valor):
    try:
        float(str(valor).replace(',', '.'))
        return True
    except Exception:
        return False


def _timbrar_factura(factura_id):
    """Timbra una factura borrador vía el PAC configurado y actualiza el estado."""
    factura = Factura.query.get(factura_id)
    if not factura or not factura.xml_content:
        return None

    from app.facturacion_cfdi import timbrar
    from satcfdi.cfdi import CFDI

    try:
        cfdi = CFDI.from_string(factura.xml_content)
        uuid, xml_timbrado = timbrar(cfdi)
        factura.uuid = uuid
        factura.xml_timbrado = xml_timbrado.decode('utf-8')
        factura.fecha_timbrado = datetime.utcnow()
        factura.estado = 'timbrada'
        factura.mensaje = 'Comprobante timbrado correctamente.'
        db.session.commit()
        return uuid
    except Exception as e:
        factura.estado = 'borrador'
        factura.mensaje = f'Error al timbrar: {e}'
        db.session.commit()
        current_app.logger.error(f'[FACTURACION] Error al timbrar {factura.id}: {e}', exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Historial de facturas
# --------------------------------------------------------------------------- #

@facturacion_bp.route('/facturacion/facturas')
@login_required
def facturas():
    lista = Factura.query.filter_by(user_id=current_user.id)\
        .order_by(Factura.created_at.desc()).all()
    return render_template('facturacion/facturas.html', facturas=lista)


@facturacion_bp.route('/facturacion/facturas/<int:factura_id>')
@login_required
def detalle(factura_id):
    factura = Factura.query.get_or_404(factura_id)
    if factura.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('facturacion.facturas'))
    concepto = None
    if factura.concepto_json:
        try:
            concepto = json.loads(factura.concepto_json)
        except Exception:
            pass
    return render_template('facturacion/factura_detalle.html', factura=factura, concepto=concepto)


@facturacion_bp.route('/facturacion/facturas/<int:factura_id>/timbrar', methods=['POST'])
@login_required
def timbrar_factura(factura_id):
    factura = Factura.query.get_or_404(factura_id)
    if factura.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('facturacion.facturas'))
    if factura.estado == 'timbrada':
        flash('La factura ya esta timbrada.', 'warning')
        return redirect(url_for('facturacion.detalle', factura_id=factura.id))
    uuid = _timbrar_factura(factura.id)
    if uuid:
        flash(f'Factura timbrada: UUID {uuid}', 'success')
    else:
        flash('No se pudo timbrar la factura. Revisa el mensaje.', 'error')
    return redirect(url_for('facturacion.detalle', factura_id=factura.id))


@facturacion_bp.route('/facturacion/facturas/<int:factura_id>/xml')
@login_required
def descargar_xml(factura_id):
    factura = Factura.query.get_or_404(factura_id)
    if factura.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('facturacion.facturas'))

    xml = factura.xml_timbrado or factura.xml_content or ''
    nombre = f'CFDI_{factura.folio or factura.id}.xml'
    return Response(
        xml,
        mimetype='application/xml',
        headers={'Content-Disposition': f'attachment; filename={nombre}'},
    )


@facturacion_bp.route('/facturacion/facturas/<int:factura_id>/eliminar', methods=['POST'])
@login_required
def eliminar_factura(factura_id):
    factura = Factura.query.get_or_404(factura_id)
    if factura.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('facturacion.facturas'))
    db.session.delete(factura)
    db.session.commit()
    flash('Factura eliminada.', 'success')
    return redirect(url_for('facturacion.facturas'))


def _factura_o_redirect(factura_id):
    factura = Factura.query.get_or_404(factura_id)
    if factura.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return None, redirect(url_for('facturacion.facturas'))
    return factura, None


def _extraer_timbre(xml_text):
    """Devuelve (uuid, fecha_timbrado) desde el XML timbrado por el SAT."""
    from lxml import etree
    from decimal import Decimal, InvalidOperation

    try:
        root = etree.fromstring(xml_text.encode('utf-8'))
    except etree.XMLSyntaxError as e:
        raise Exception(f'El XML capturado no es valido: {e}')

    tag = etree.QName(root).localname if hasattr(root.tag, 'text') else str(root.tag).rsplit('}', 1)[-1]
    if tag != 'Comprobante':
        raise Exception('El XML no parece un CFDI (falta el nodo Comprobante).')

    timbre = None
    for el in root.iter():
        local = str(el.tag).rsplit('}', 1)[-1]
        if local == 'TimbreFiscalDigital':
            timbre = el
            break
    if timbre is None:
        raise Exception('El XML no contiene el nodo TimbreFiscalDigital; verifica que sea el XML timbrado por el SAT.')

    uuid_val = timbre.get('UUID', '').strip()
    fecha_timbrado = timbre.get('FechaTimbrado', '').strip() or None
    if not uuid_val:
        raise Exception('El TimbreFiscalDigital no tiene UUID.')

    return uuid_val, fecha_timbrado


@facturacion_bp.route('/facturacion/facturas/<int:factura_id>/guia')
@login_required
def guia_captura(factura_id):
    factura, redir = _factura_o_redirect(factura_id)
    if redir:
        return redir
    concepto = None
    if factura.concepto_json:
        try:
            concepto = json.loads(factura.concepto_json)
        except Exception:
            pass
    empresa = db.session.get(Empresa, factura.empresa_id)
    return render_template('facturacion/guia_captura.html', factura=factura, concepto=concepto, empresa=empresa)


@facturacion_bp.route('/facturacion/facturas/<int:factura_id>/timbrado-manual', methods=['POST'])
@login_required
def registrar_timbrado_manual(factura_id):
    factura, redir = _factura_o_redirect(factura_id)
    if redir:
        return redir

    xml_text = (request.form.get('xml_timbrado') or '').strip()
    uuid_text = (request.form.get('uuid') or '').strip()

    if not xml_text and not uuid_text:
        flash('Indica el UUID o pega el XML timbrado.', 'error')
        return redirect(url_for('facturacion.guia_captura', factura_id=factura.id))

    uuid_val = None
    fecha_timbrado = None
    xml_normalizado = None
    try:
        if xml_text:
            uuid_val, fecha_timbrado = _extraer_timbre(xml_text)
            xml_normalizado = xml_text
            from lxml import etree
            xml_normalizado = etree.tostring(etree.fromstring(xml_text.encode('utf-8')),
                                             pretty_print=True, encoding='unicode')
            if uuid_text:
                try:
                    uuid_text_canon = str(uuidmod.UUID(uuid_text))
                except Exception:
                    flash('El UUID capturado no tiene el formato correcto.', 'error')
                    return redirect(url_for('facturacion.guia_captura', factura_id=factura.id))
                if uuid_text_canon != str(uuidmod.UUID(uuid_val)):
                    flash('El UUID capturado no coincide con el UUID del XML.', 'error')
                    return redirect(url_for('facturacion.guia_captura', factura_id=factura.id))
        else:
            uuid_val = str(uuidmod.UUID(uuid_text))
    except Exception as e:
        flash(f'No se pudo registrar el timbre: {e}', 'error')
        return redirect(url_for('facturacion.guia_captura', factura_id=factura.id))

    if not fecha_timbrado:
        from satcfdi.cfdi import MEXICO_TZ
        fecha_timbrado = datetime.now(MEXICO_TZ)
    else:
        try:
            fecha_timbrado = datetime.fromisoformat(str(fecha_timbrado).replace('Z', '+00:00'))
        except Exception:
            from satcfdi.cfdi import MEXICO_TZ
            fecha_timbrado = datetime.now(MEXICO_TZ)

    factura.uuid = uuid_val
    factura.fecha_timbrado = fecha_timbrado
    factura.xml_timbrado = xml_normalizado
    factura.estado = 'timbrada'
    factura.mensaje = 'Timbrada manualmente en el portal gratuito del SAT.'
    db.session.commit()

    flash(f'Factura registrada como timbrada (UUID {uuid_val}).', 'success')
    return redirect(url_for('facturacion.detalle', factura_id=factura.id))