import os
import time
import base64
import io
import zipfile
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Empresa, FielCredentials, CFDI, DownloadRequest
from app.fiel import decrypt_password

sat_bp = Blueprint('sat', __name__)


def _create_signer(empresa):
    from OpenSSL.crypto import load_certificate, FILETYPE_PEM
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    fiel = FielCredentials.query.filter_by(empresa_id=empresa.id).first()
    if not fiel:
        return None, 'No hay credenciales FIEL configuradas.'

    password = decrypt_password(fiel.password_encrypted)

    cer_data = fiel.cer_data
    key_data = fiel.key_data
    if not cer_data or not key_data:
        # Fallback: archivos en disco (registros creados antes de guardar en BD)
        fiel_dir = os.path.join(current_app.config['UPLOAD_FOLDER_FIEL'], empresa.rfc)
        cer_path = os.path.join(fiel_dir, fiel.cer_filename)
        key_path = os.path.join(fiel_dir, fiel.key_filename)
        if not os.path.exists(cer_path) or not os.path.exists(key_path):
            return None, 'Archivos FIEL no encontrados.'
        with open(cer_path, 'rb') as f:
            cer_data = f.read()
        with open(key_path, 'rb') as f:
            key_data = f.read()

    try:
        from satcfdi.models.signer import Signer
        signer = Signer.load(
            certificate=cer_data,
            key=key_data,
            password=password.encode() if password else None,
        )
        return signer, None
    except Exception as e:
        return None, f'Error al cargar FIEL: {str(e)}'


def _parse_cfdi_metadata(xml_bytes, tipo_solicitud):
    from lxml import etree
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_bytes)
        ns = {'cfdi': 'http://www.sat.gob.mx/cfd/4', 'cfdi33': 'http://www.sat.gob.mx/cfd/3'}

        comprobante = root
        if comprobante.tag == '{http://www.sat.gob.mx/cfd/4}Comprobante':
            nsmap = ns['cfdi']
        elif comprobante.tag == '{http://www.sat.gob.mx/cfd/3}Comprobante':
            nsmap = ns['cfdi33']
        else:
            nsmap = None

        if nsmap is None:
            nsmap = root.tag.split('}')[0].strip('{') if '}' in root.tag else ''

        emisor = root.find(f'{{{nsmap}}}Emisor') if nsmap else root.find('Emisor')
        receptor = root.find(f'{{{nsmap}}}Receptor') if nsmap else root.find('Receptor')
        impuestos = root.find(f'{{{nsmap}}}Impuestos') if nsmap else root.find('Impuestos')
        timbrado = None
        complemento = root.find(f'{{{nsmap}}}Complemento') if nsmap else root.find('Complemento')
        if complemento is not None:
            timbrado = complemento.find('{http://www.sat.gob.mx/TimbreFiscalDigital}TimbreFiscalDigital')

        uuid = ''
        fecha_timbrado = None
        if timbrado is not None:
            uuid = timbrado.get('UUID', '')
            fecha_timbrado = timbrado.get('FechaTimbrado', '')

        rfc_emisor = emisor.get('Rfc', '') if emisor is not None else ''
        nombre_emisor = emisor.get('Nombre', '') if emisor is not None else ''
        rfc_receptor = receptor.get('Rfc', '') if receptor is not None else ''
        nombre_receptor = receptor.get('Nombre', '') if receptor is not None else ''

        subtotal = float(root.get('SubTotal', 0))
        total = float(root.get('Total', 0))
        fecha = root.get('Fecha', '')
        tipo_comp = root.get('TipoDeComprobante', '')
        serie = root.get('Serie', '')
        folio = root.get('Folio', '')
        uso_cfdi = receptor.get('UsoCFDI', '') if receptor is not None else ''
        metodo_pago = root.get('MetodoPago', '')
        forma_pago = root.get('FormaPago', '')
        moneda = root.get('Moneda', 'MXN')
        tipo_cambio = float(root.get('TipoCambio', 1))

        total_impuestos = 0
        if impuestos is not None:
            total_impuestos = float(impuestos.get('TotalImpuestosTrasladados', 0)) + \
                            float(impuestos.get('TotalImpuestosRetenidos', 0))

        estado = 'vigente'

        return {
            'uuid': uuid,
            'tipo_comprobante': tipo_comp,
            'fecha_emision': fecha,
            'fecha_timbrado': fecha_timbrado,
            'rfc_emisor': rfc_emisor,
            'nombre_emisor': nombre_emisor,
            'rfc_receptor': rfc_receptor,
            'nombre_receptor': nombre_receptor,
            'subtotal': subtotal,
            'total': total,
            'impuestos': total_impuestos,
            'estado': estado,
            'uso_cfdi': uso_cfdi,
            'metodo_pago': metodo_pago,
            'forma_pago': forma_pago,
            'serie': serie,
            'folio': folio,
            'moneda': moneda,
            'tipo_cambio': tipo_cambio,
        }
    except Exception:
        return None


@sat_bp.route('/sat/descargar', methods=['GET', 'POST'])
@login_required
def descargar_cfdi():
    empresas = Empresa.query.filter_by(user_id=current_user.id, activa=True).all()

    if request.method == 'POST':
        empresa_id = request.form.get('empresa_id', type=int)
        tipo = request.form.get('tipo', 'emitidos')
        fecha_inicio = request.form.get('fecha_inicio')
        fecha_fin = request.form.get('fecha_fin')

        if not empresa_id or not fecha_inicio or not fecha_fin:
            flash('Complete todos los campos.', 'error')
            return render_template('sat/descargar.html', empresas=empresas)

        empresa = Empresa.query.get_or_404(empresa_id)
        if empresa.user_id != current_user.id:
            flash('No tienes acceso a esta empresa.', 'error')
            return redirect(url_for('sat.descargar_cfdi'))

        request_dl = DownloadRequest(
            empresa_id=empresa.id,
            tipo=tipo,
            fecha_inicio=datetime.strptime(fecha_inicio, '%Y-%m-%d'),
            fecha_fin=datetime.strptime(fecha_fin, '%Y-%m-%d'),
            estado='procesando'
        )
        db.session.add(request_dl)
        db.session.commit()

        try:
            signer, error = _create_signer(empresa)
            if error:
                raise Exception(error)

            from satcfdi.pacs.sat import SAT, EstadoSolicitud, EstadoComprobante

            try:
                fecha_inicio_d = datetime.strptime(fecha_inicio, '%Y-%m-%d').date()
                fecha_fin_d = datetime.strptime(fecha_fin, '%Y-%m-%d').date()
            except ValueError:
                raise Exception('Formato de fechas invalido (use YYYY-MM-DD).')

            sat = SAT(signer=signer)

            if tipo == 'recibidos':
                def solicitar(estado):
                    return sat.recover_comprobante_received_request(
                        fecha_inicial=fecha_inicio_d, fecha_final=fecha_fin_d,
                        tipo_solicitud='CFDI', estado_comprobante=estado)
            elif tipo == 'retenciones_emitidas':
                def solicitar(estado):
                    return sat.recover_retencion_emitted_request(
                        fecha_inicial=fecha_inicio_d, fecha_final=fecha_fin_d,
                        tipo_solicitud='CFDI', estado_comprobante=estado)
            elif tipo == 'retenciones_recibidas':
                def solicitar(estado):
                    return sat.recover_retencion_received_request(
                        fecha_inicial=fecha_inicio_d, fecha_final=fecha_fin_d,
                        tipo_solicitud='CFDI', estado_comprobante=estado)
            else:
                def solicitar(estado):
                    return sat.recover_comprobante_emitted_request(
                        fecha_inicial=fecha_inicio_d, fecha_final=fecha_fin_d,
                        tipo_solicitud='CFDI', estado_comprobante=estado)

            # El SAT solo acepta 'Vigente' para descarga de CFDI; intentamos
            # 'Todos' y si lo rechaza reintentamos con 'Vigente'.
            solicitud = None
            ultimo_error = None
            for estado in (EstadoComprobante.TODOS, EstadoComprobante.VIGENTE):
                try:
                    solicitud = solicitar(estado)
                    if solicitud.get('CodEstatus', '5000') == '5000':
                        break
                    if solicitud.get('CodEstatus') == '5004':
                        break  # sin resultados para la consulta
                    ultimo_error = solicitud
                except Exception as e:
                    ultimo_error = e
            if solicitud is None:
                raise Exception(f'El SAT rechazo la solicitud de descarga: {ultimo_error}')

            if solicitud.get('CodEstatus') == '5004':
                request_dl.estado = 'completado'
                request_dl.total_descargados = 0
                request_dl.mensaje = 'No se encontraron CFDIs con esos parametros.'
                request_dl.completed_at = datetime.utcnow()
                db.session.commit()
                flash('Descarga completada: 0 CFDIs (el SAT no encontro coincidencias).', 'success')
                return redirect(url_for('sat.descargar_cfdi'))

            id_solicitud = solicitud['IdSolicitud']

            # Esperar a que el SAT genere los paquetes (suele tardar 1-2 minutos)
            st = None
            for _ in range(25):
                time.sleep(15)
                st = sat.recover_comprobante_status(id_solicitud)
                estado_solicitud = st.get('EstadoSolicitud')
                if estado_solicitud == int(EstadoSolicitud.TERMINADA):
                    break
                if estado_solicitud in (int(EstadoSolicitud.ERROR), int(EstadoSolicitud.RECHAZADA), int(EstadoSolicitud.VENCIDA)):
                    raise Exception(f'El SAT rechazo la solicitud de descarga: {st}')
            else:
                raise Exception('El SAT tarda demasiado; la solicitud sigue en proceso.')

            count = 0
            for id_paquete in (st.get('IdsPaquetes') or []):
                _, paquete_b64 = sat.recover_comprobante_download(id_paquete)
                if not paquete_b64:
                    continue
                with zipfile.ZipFile(io.BytesIO(base64.b64decode(paquete_b64))) as paquete:
                    for nombre in paquete.namelist():
                        if not nombre.lower().endswith('.xml'):
                            continue
                        xml_bytes = paquete.read(nombre)
                        metadata = _parse_cfdi_metadata(xml_bytes, tipo)
                        if not metadata or not metadata['uuid']:
                            continue
                        existing = CFDI.query.filter_by(
                            empresa_id=empresa.id,
                            uuid=metadata['uuid']
                        ).first()
                        if existing:
                            continue

                        cf = CFDI(
                            empresa_id=empresa.id,
                            uuid=metadata['uuid'],
                            tipo_comprobante=metadata['tipo_comprobante'],
                            fecha_emision=datetime.fromisoformat(metadata['fecha_emision'].replace('T', ' ')) if metadata['fecha_emision'] else None,
                            fecha_timbrado=datetime.fromisoformat(metadata['fecha_timbrado'].replace('T', ' ')) if metadata.get('fecha_timbrado') else None,
                            rfc_emisor=metadata['rfc_emisor'],
                            nombre_emisor=metadata['nombre_emisor'],
                            rfc_receptor=metadata['rfc_receptor'],
                            nombre_receptor=metadata['nombre_receptor'],
                            subtotal=metadata['subtotal'],
                            total=metadata['total'],
                            impuestos=metadata['impuestos'],
                            estado=metadata['estado'],
                            uso_cfdi=metadata['uso_cfdi'],
                            metodo_pago=metadata['metodo_pago'],
                            forma_pago=metadata['forma_pago'],
                            serie=metadata.get('serie', ''),
                            folio=metadata.get('folio', ''),
                            moneda=metadata['moneda'],
                            tipo_cambio=metadata['tipo_cambio'],
                            xml_content=xml_bytes.decode('utf-8', errors='ignore'),
                        )
                        db.session.add(cf)
                        count += 1

            request_dl.estado = 'completado'
            request_dl.total_descargados = count
            request_dl.completed_at = datetime.utcnow()
            db.session.commit()

            flash(f'Descarga completada: {count} CFDIs sincronizados.', 'success')

        except ImportError as e:
            request_dl.estado = 'error'
            request_dl.mensaje = f'Error de importacion: {str(e)}'
            db.session.commit()
            flash('Error: Verifique que satcfdi este instalado: pip install satcfdi', 'error')

        except Exception as e:
            request_dl.estado = 'error'
            request_dl.mensaje = str(e)
            db.session.commit()
            flash(f'Error en la descarga: {str(e)}', 'error')

        return redirect(url_for('sat.descargar_cfdi'))

    return render_template('sat/descargar.html', empresas=empresas)


@sat_bp.route('/sat/sincronizar-metadata/<int:empresa_id>', methods=['POST'])
@login_required
def sincronizar_metadata(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    if empresa.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('dashboard.index'))

    try:
        signer, error = _create_signer(empresa)
        if error:
            raise Exception(error)

        from satcfdi.pacs.sat import SAT
        sat = SAT(signer=signer)

        cfdis = CFDI.query.filter_by(empresa_id=empresa.id).all()
        actualizados = 0

        for cf in cfdis:
            try:
                consulta = {
                    'Emisor': {'Rfc': cf.rfc_emisor},
                    'Receptor': {'Rfc': cf.rfc_receptor},
                    'Total': cf.total,
                    'Complemento': {'TimbreFiscalDigital': {'UUID': cf.uuid}},
                }
                res = sat.status(consulta)
                estado_sat = res.get('Estado', '')
                if 'Cancelado' in estado_sat and cf.estado != 'cancelado':
                    cf.estado = 'cancelado'
                    actualizados += 1
                elif 'Vigente' in estado_sat and cf.estado != 'vigente':
                    cf.estado = 'vigente'
                    actualizados += 1
            except Exception:
                continue

        db.session.commit()
        flash(f'Metadata sincronizada: {actualizados} CFDIs actualizados.', 'success')

    except ImportError:
        flash('Libreria satcfdi no instalada.', 'error')
    except Exception as e:
        flash(f'Error al sincronizar: {str(e)}', 'error')

    return redirect(url_for('dashboard.ver_empresa', empresa_id=empresa_id))


@sat_bp.route('/api/cfdis/<int:empresa_id>')
@login_required
def api_cfdis(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    if empresa.user_id != current_user.id:
        return jsonify({'error': 'No access'}), 403

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    tipo = request.args.get('tipo', '')
    estado = request.args.get('estado', '')
    search = request.args.get('search', '')

    query = CFDI.query.filter_by(empresa_id=empresa.id)

    if tipo:
        query = query.filter_by(tipo_comprobante=tipo)
    if estado:
        query = query.filter_by(estado=estado)
    if search:
        search_filter = f'%{search}%'
        query = query.filter(
            db.or_(
                CFDI.uuid.ilike(search_filter),
                CFDI.rfc_emisor.ilike(search_filter),
                CFDI.rfc_receptor.ilike(search_filter),
                CFDI.nombre_emisor.ilike(search_filter),
                CFDI.nombre_receptor.ilike(search_filter),
            )
        )

    pagination = query.order_by(CFDI.fecha_emision.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    cfdis = [{
        'id': c.id,
        'uuid': c.uuid,
        'tipo': c.tipo_comprobante,
        'fecha_emision': c.fecha_emision.isoformat() if c.fecha_emision else None,
        'rfc_emisor': c.rfc_emisor,
        'nombre_emisor': c.nombre_emisor,
        'rfc_receptor': c.rfc_receptor,
        'nombre_receptor': c.nombre_receptor,
        'subtotal': c.subtotal,
        'total': c.total,
        'impuestos': c.impuestos,
        'estado': c.estado,
        'uso_cfdi': c.uso_cfdi,
        'moneda': c.moneda,
    } for c in pagination.items]

    return jsonify({
        'cfdis': cfdis,
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page
    })
