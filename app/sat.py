import os
import time
import base64
import io
import zipfile
import threading
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, current_app, Response
from flask_login import login_required, current_user
from app import db
from app.models import Empresa, FielCredentials, CFDI, DownloadRequest
from app.fiel import decrypt_password

sat_bp = Blueprint('sat', __name__)


def _create_signer(empresa):
    from OpenSSL.crypto import load_certificate, FILETYPE_PEM, FILETYPE_ASN1
    from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_der_private_key

    fiel = FielCredentials.query.filter_by(empresa_id=empresa.id).first()
    if not fiel:
        return None, 'No se encontraron credenciales FIEL para esta empresa.'

    if not fiel.cer_data or not fiel.key_data:
        return None, 'FIEL incompleta: suba de nuevo los archivos .cer y .key.'

    try:
        cer_raw = fiel.cer_data if isinstance(fiel.cer_data, bytes) else fiel.cer_data.encode('latin-1')
        key_raw = fiel.key_data if isinstance(fiel.key_data, bytes) else fiel.key_data.encode('latin-1')

        is_pem_cert = b'-----BEGIN CERTIFICATE-----' in cer_raw
        is_pem_key = b'-----BEGIN' in key_raw

        if is_pem_cert:
            cert = load_certificate(FILETYPE_PEM, cer_raw)
        else:
            cert = load_certificate(FILETYPE_ASN1, cer_raw)

        password = decrypt_password(fiel.password_encrypted)
        password_bytes = password.encode('utf-8') if password else None

        if is_pem_key:
            private_key = load_pem_private_key(key_raw, password=password_bytes)
        else:
            private_key = load_der_private_key(key_raw, password=password_bytes)

        from satcfdi.models.signer import Signer
        signer = Signer(certificate=cert, key=private_key)
        return signer, None
    except Exception as e:
        return None, f'Error al cargar FIEL: {str(e)}'


def _parse_cfdi_metadata(xml_bytes, tipo_solicitud='emitidos'):
    import xml.etree.ElementTree as ET
    import json

    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return None

    nsmap = None
    if root.tag.startswith('{'):
        nsmap = root.tag.split('}')[0].strip('{')

    def find(elem, tag):
        return elem.find(f'{{{nsmap}}}{tag}') if nsmap else elem.find(tag)

    def findall(elem, tag):
        return elem.findall(f'{{{nsmap}}}{tag}') if nsmap else elem.findall(tag)

    def attr(node, name, default=''):
        return node.get(name, default) if node is not None else default

    rfc_emisor = attr(find(root, 'Emisor'), 'Rfc')
    nombre_emisor = attr(find(root, 'Emisor'), 'Nombre')
    rfc_receptor = attr(find(root, 'Receptor'), 'Rfc')
    nombre_receptor = attr(find(root, 'Receptor'), 'Nombre')

    uuid = ''
    fecha_timbrado = ''
    complemento = find(root, 'Complemento')
    if complemento is not None:
        for child in complemento:
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag == 'TimbreFiscalDigital':
                uuid = child.get('UUID', '')
                fecha_timbrado = child.get('FechaTimbrado', '')
                break

    tipo_comp = root.get('TipoDeComprobante', 'I')

    impuestos_total = float(root.get('TotalImpuestosTrasladados', 0) or 0)
    impuestos_elem = find(root, 'Impuestos')

    iva_trasladado = 0
    isr_retenido = 0
    iva_retenido = 0

    if impuestos_elem is not None:
        traslados = find(impuestos_elem, 'Traslados')
        if traslados is not None:
            for t in findall(traslados, 'Traslado'):
                if t.get('Impuesto') == '002':
                    iva_trasladado += float(t.get('Importe', 0))

        retenciones = find(impuestos_elem, 'Retenciones')
        if retenciones is not None:
            for r in findall(retenciones, 'Retencion'):
                imp_code = r.get('Impuesto', '')
                importe = float(r.get('Importe', 0))
                if imp_code == '001':
                    isr_retenido += importe
                elif imp_code == '002':
                    iva_retenido += importe

    conceptos = []
    conceptos_elem = find(root, 'Conceptos')
    if conceptos_elem is not None:
        for c in findall(conceptos_elem, 'Concepto'):
            conceptos.append({
                'claveProdServ': c.get('ClaveProdServ', ''),
                'cantidad': float(c.get('Cantidad', 0)),
                'claveUnidad': c.get('ClaveUnidad', ''),
                'descripcion': c.get('Descripcion', ''),
                'valorUnitario': float(c.get('ValorUnitario', 0)),
                'importe': float(c.get('Importe', 0)),
            })

    subtotal = float(root.get('SubTotal', 0) or 0)
    total = float(root.get('Total', 0) or 0)

    try:
        return {
            'uuid': uuid,
            'tipo_comprobante': tipo_comp,
            'fecha_emision': root.get('Fecha', ''),
            'fecha_timbrado': fecha_timbrado,
            'rfc_emisor': rfc_emisor,
            'nombre_emisor': nombre_emisor,
            'rfc_receptor': rfc_receptor,
            'nombre_receptor': nombre_receptor,
            'subtotal': subtotal,
            'total': total,
            'impuestos': impuestos_total,
            'iva_trasladado': iva_trasladado,
            'isr_retenido': isr_retenido,
            'iva_retenido': iva_retenido,
            'estado': 'vigente',
            'uso_cfdi': attr(find(root, 'Receptor'), 'UsoCFDI'),
            'metodo_pago': root.get('MetodoPago', ''),
            'forma_pago': root.get('FormaPago', ''),
            'serie': root.get('Serie', ''),
            'folio': root.get('Folio', ''),
            'moneda': root.get('Moneda', 'MXN'),
            'tipo_cambio': root.get('TipoCambio', '1'),
            'conceptos': conceptos,
        }
    except Exception:
        return None


@sat_bp.route('/sat/probar-conexion/<int:empresa_id>', methods=['POST'])
@login_required
def probar_conexion(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    if empresa.user_id != current_user.id:
        return jsonify({'ok': False, 'error': 'No tienes acceso.'}), 403

    try:
        signer, error = _create_signer(empresa)
        if error:
            return jsonify({'ok': False, 'error': error}), 400

        from satcfdi.pacs.sat import SAT
        sat = SAT(signer=signer)

        from datetime import date, timedelta
        hoy = date.today()
        inicio = hoy - timedelta(days=7)

        solicitud = sat.recover_comprobante_emitted_request(
            fecha_inicial=inicio, fecha_final=hoy,
            tipo_solicitud='CFDI')

        cod = solicitud.get('CodEstatus', '')
        id_sol = solicitud.get('IdSolicitud', '')
        msg = solicitud.get('Mensaje', '')

        if cod == '5004':
            return jsonify({'ok': True, 'mensaje': 'Conexion SAT exitosa. No hay CFDIs emitidos en los ultimos 7 dias.', 'id_solicitud': id_sol})
        elif cod in ('5000', ''):
            return jsonify({'ok': True, 'mensaje': f'Conexion SAT exitosa. Solicitud {id_sol} creada correctamente.', 'id_solicitud': id_sol})
        else:
            return jsonify({'ok': False, 'error': f'SAT respondio CodEstatus {cod}: {msg}', 'respuesta': solicitud}), 400

    except ImportError:
        return jsonify({'ok': False, 'error': 'satcfdi no esta instalado en el servidor.'}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@sat_bp.route('/sat/descargar', methods=['GET', 'POST'])
@login_required
def descargar_cfdi():
    empresas = Empresa.query.filter_by(user_id=current_user.id, activa=True).all()

    if request.method == 'POST':
        empresa_id = request.form.get('empresa_id', type=int)
        tipo = request.form.get('tipo', 'emitidos')
        fecha_inicio = request.form.get('fecha_inicio')
        fecha_fin = request.form.get('fecha_fin')
        incluir_pdf = request.form.get('incluir_pdf') == '1'

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

        request_id = request_dl.id
        empresa_id_val = empresa.id
        app = current_app._get_current_object()

        thread = threading.Thread(
            target=_ejecutar_descarga_sat,
            args=(app, request_id, empresa_id_val, tipo, fecha_inicio, fecha_fin, incluir_pdf),
            daemon=True
        )
        thread.start()

        flash('Descarga en proceso. El SAT tarda 1-2 min. Revisa el dashboard para ver el resultado.', 'success')
        return redirect(url_for('sat.descargar_cfdi'))

    return render_template('sat/descargar.html', empresas=empresas)


def _ejecutar_descarga_sat(app, request_id, empresa_id, tipo, fecha_inicio, fecha_fin, incluir_pdf):
    import logging
    logger = logging.getLogger(__name__)

    with app.app_context():
        request_dl = DownloadRequest.query.get(request_id)
        if not request_dl:
            logger.error(f"[BG] DownloadRequest {request_id} no encontrado")
            return

        empresa = Empresa.query.get(empresa_id)
        if not empresa:
            request_dl.estado = 'error'
            request_dl.mensaje = 'Empresa no encontrada.'
            db.session.commit()
            return

        try:
            logger.info(f"[BG] Iniciando descarga empresa={empresa.rfc} tipo={tipo} periodo={fecha_inicio}~{fecha_fin}")
            signer, error = _create_signer(empresa)
            if error:
                raise Exception(error)

            from satcfdi.pacs.sat import SAT, EstadoSolicitud, EstadoComprobante

            fecha_inicio_d = datetime.strptime(fecha_inicio, '%Y-%m-%d').date()
            fecha_fin_d = datetime.strptime(fecha_fin, '%Y-%m-%d').date()

            sat = SAT(signer=signer)
            logger.info(f"[BG] SAT signer creado OK, solicitando descarga...")

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

            solicitud = None
            ultimo_error = None
            for estado in (EstadoComprobante.TODOS, EstadoComprobante.VIGENTE):
                try:
                    solicitud = solicitar(estado)
                    if solicitud.get('CodEstatus', '5000') == '5000':
                        break
                    if solicitud.get('CodEstatus') == '5004':
                        break
                    ultimo_error = solicitud
                except Exception as e:
                    ultimo_error = e
            if solicitud is None:
                raise Exception(f'El SAT rechazo la solicitud de descarga: {ultimo_error}')

            logger.info(f"[BG] Solicitud SAT respuesta: {solicitud}")

            if solicitud.get('CodEstatus') == '5004':
                request_dl.estado = 'completado'
                request_dl.total_descargados = 0
                request_dl.mensaje = 'No se encontraron CFDIs con esos parametros.'
                request_dl.completed_at = datetime.utcnow()
                db.session.commit()
                return

            id_solicitud = solicitud['IdSolicitud']
            logger.info(f"[BG] ID solicitud: {id_solicitud}, esperando procesamiento del SAT...")

            st = None
            for _ in range(25):
                time.sleep(15)
                st = sat.recover_comprobante_status(id_solicitud)
                estado_solicitud = st.get('EstadoSolicitud')
                if estado_solicitud == int(EstadoSolicitud.TERMINADA):
                    break
                if estado_solicitud == int(EstadoSolicitud.RECHAZADA):
                    num_cfdis = st.get('NumeroCFDIs', 0)
                    id_paq = st.get('IdsPaquetes') or []
                    if num_cfdis == 0 and len(id_paq) == 0:
                        request_dl.estado = 'completado'
                        request_dl.total_descargados = 0
                        request_dl.mensaje = 'No se encontraron CFDIs con esos parametros.'
                        request_dl.completed_at = datetime.utcnow()
                        db.session.commit()
                        return
                    raise Exception(f'El SAT rechazo la solicitud de descarga: {st}')
                if estado_solicitud in (int(EstadoSolicitud.ERROR), int(EstadoSolicitud.VENCIDA)):
                    raise Exception(f'El SAT rechazo la solicitud de descarga: {st}')
            else:
                raise Exception('El SAT tarda demasiado; la solicitud sigue en proceso.')

            logger.info(f"[BG] SAT terminado: paquetes={st.get('IdsPaquetes') or []}")

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
                            iva_trasladado=metadata['iva_trasladado'],
                            isr_retenido=metadata['isr_retenido'],
                            iva_retenido=metadata['iva_retenido'],
                            estado=metadata['estado'],
                            uso_cfdi=metadata['uso_cfdi'],
                            metodo_pago=metadata['metodo_pago'],
                            forma_pago=metadata['forma_pago'],
                            serie=metadata.get('serie', ''),
                            folio=metadata.get('folio', ''),
                            moneda=metadata['moneda'],
                            tipo_cambio=metadata['tipo_cambio'],
                            xml_content=xml_bytes.decode('utf-8', errors='ignore'),
                            conceptos_json=__import__('json').dumps(metadata.get('conceptos', []), ensure_ascii=False) if metadata.get('conceptos') else None,
                        )
                        db.session.add(cf)
                        count += 1

            pdf_count = 0
            if incluir_pdf and count > 0:
                try:
                    if tipo == 'recibidos':
                        def solicitar_pdf(estado):
                            return sat.recover_comprobante_received_request(
                                fecha_inicial=fecha_inicio_d, fecha_final=fecha_fin_d,
                                tipo_solicitud='PDF', estado_comprobante=estado)
                    elif tipo == 'retenciones_emitidas':
                        def solicitar_pdf(estado):
                            return sat.recover_retencion_emitted_request(
                                fecha_inicial=fecha_inicio_d, fecha_final=fecha_fin_d,
                                tipo_solicitud='PDF', estado_comprobante=estado)
                    elif tipo == 'retenciones_recibidas':
                        def solicitar_pdf(estado):
                            return sat.recover_retencion_received_request(
                                fecha_inicial=fecha_inicio_d, fecha_final=fecha_fin_d,
                                tipo_solicitud='PDF', estado_comprobante=estado)
                    else:
                        def solicitar_pdf(estado):
                            return sat.recover_comprobante_emitted_request(
                                fecha_inicial=fecha_inicio_d, fecha_final=fecha_fin_d,
                                tipo_solicitud='PDF', estado_comprobante=estado)

                    solicitud_pdf = None
                    for estado in (EstadoComprobante.TODOS, EstadoComprobante.VIGENTE):
                        try:
                            solicitud_pdf = solicitar_pdf(estado)
                            if solicitud_pdf.get('CodEstatus', '5000') == '5000':
                                break
                            if solicitud_pdf.get('CodEstatus') == '5004':
                                break
                        except Exception:
                            continue

                    if solicitud_pdf and solicitud_pdf.get('CodEstatus') != '5004':
                        id_solicitud_pdf = solicitud_pdf['IdSolicitud']
                        for _ in range(25):
                            time.sleep(15)
                            st_pdf = sat.recover_comprobante_status(id_solicitud_pdf)
                            if st_pdf.get('EstadoSolicitud') == int(EstadoSolicitud.TERMINADA):
                                break
                            if st_pdf.get('EstadoSolicitud') in (int(EstadoSolicitud.ERROR), int(EstadoSolicitud.RECHAZADA), int(EstadoSolicitud.VENCIDA)):
                                break
                        else:
                            st_pdf = st_pdf if 'st_pdf' in dir() else None

                        if st_pdf and st_pdf.get('EstadoSolicitud') == int(EstadoSolicitud.TERMINADA):
                            import json as _json
                            cfdis_empresa = {cf.uuid: cf for cf in CFDI.query.filter_by(empresa_id=empresa.id).all()}
                            upload_dir = os.path.join(app.config.get('UPLOAD_FOLDER_CFDIS', 'app/uploads/cfdis'), str(empresa.id))
                            os.makedirs(upload_dir, exist_ok=True)

                            for id_paquete_pdf in (st_pdf.get('IdsPaquetes') or []):
                                _, paquete_pdf_b64 = sat.recover_comprobante_download(id_paquete_pdf)
                                if not paquete_pdf_b64:
                                    continue
                                try:
                                    with zipfile.ZipFile(io.BytesIO(base64.b64decode(paquete_pdf_b64))) as paquete_pdf:
                                        for nombre_pdf in paquete_pdf.namelist():
                                            nombre_lower = nombre_pdf.lower()
                                            if not nombre_lower.endswith('.pdf'):
                                                continue
                                            pdf_bytes = paquete_pdf.read(nombre_pdf)
                                            uuid_pdf = nombre_pdf.rsplit('.', 1)[0]
                                            cf_pdf = cfdis_empresa.get(uuid_pdf)
                                            if cf_pdf:
                                                pdf_file = os.path.join(upload_dir, f'{uuid_pdf}.pdf')
                                                with open(pdf_file, 'wb') as f:
                                                    f.write(pdf_bytes)
                                                cf_pdf.pdf_path = f'{uuid_pdf}.pdf'
                                                pdf_count += 1
                                except zipfile.BadZipFile:
                                    continue
                            db.session.commit()
                except Exception:
                    pass

            request_dl.estado = 'completado'
            request_dl.total_descargados = count
            request_dl.completed_at = datetime.utcnow()
            db.session.commit()
            logger.info(f"[BG] Descarga completada: {count} CFDIs descargados, {pdf_count} PDFs")

        except ImportError as e:
            request_dl.estado = 'error'
            request_dl.mensaje = f'Error de importacion: {str(e)}'
            db.session.commit()
            logger.error(f"[BG] Error de importacion: {e}", exc_info=True)

        except Exception as e:
            request_dl.estado = 'error'
            request_dl.mensaje = str(e)
            db.session.commit()
            logger.error(f"[BG] Error en descarga: {e}", exc_info=True)


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

        cfdis = CFDI.query.filter_by(empresa_id=empresa_id).all()
        updated = 0
        for cf in cfdis:
            if not cf.uuid:
                continue
            try:
                cfdi_dict = {'UUID': cf.uuid}
                status = sat.status(cfdi_dict)
                if status:
                    if 'Cancelado' in str(status):
                        cf.estado = 'cancelado'
                    updated += 1
            except Exception:
                continue

        db.session.commit()
        flash(f'Metadata sincronizada: {updated} CFDIs actualizados.', 'success')

    except Exception as e:
        flash(f'Error al sincronizar: {str(e)}', 'error')

    return redirect(url_for('dashboard.ver_empresa', empresa_id=empresa_id))


@sat_bp.route('/sat/cancelar-descarga/<int:request_id>', methods=['POST'])
@login_required
def cancelar_descarga(request_id):
    dl = DownloadRequest.query.get_or_404(request_id)
    empresa = Empresa.query.get_or_404(dl.empresa_id)
    if empresa.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('dashboard.index'))

    if dl.estado == 'procesando':
        dl.estado = 'cancelado'
        dl.mensaje = 'Cancelado por el usuario.'
        dl.completed_at = datetime.utcnow()
        db.session.commit()
        flash('Descarga cancelada.', 'success')

    return redirect(url_for('dashboard.ver_empresa', empresa_id=dl.empresa_id))


@sat_bp.route('/sat/reprocesar-descarga/<int:request_id>', methods=['POST'])
@login_required
def reprocesar_descarga(request_id):
    dl = DownloadRequest.query.get_or_404(request_id)
    empresa = Empresa.query.get_or_404(dl.empresa_id)
    if empresa.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('dashboard.index'))

    if dl.estado in ('error', 'cancelado'):
        dl.estado = 'procesando'
        dl.mensaje = None
        dl.total_descargados = 0
        dl.completed_at = None
        db.session.commit()

        empresa_id_val = dl.empresa_id
        tipo = dl.tipo
        fecha_inicio = dl.fecha_inicio.strftime('%Y-%m-%d')
        fecha_fin = dl.fecha_fin.strftime('%Y-%m-%d')
        request_id_val = dl.id
        app = current_app._get_current_object()

        thread = threading.Thread(
            target=_ejecutar_descarga_sat,
            args=(app, request_id_val, empresa_id_val, tipo, fecha_inicio, fecha_fin, False),
            daemon=True
        )
        thread.start()

        flash('Reprocesando descarga...', 'success')

    return redirect(url_for('dashboard.ver_empresa', empresa_id=dl.empresa_id))


@sat_bp.route('/sat/descargar-xml/<int:cfdi_id>')
@login_required
def descargar_xml(cfdi_id):
    from flask import Response
    cf = CFDI.query.get_or_404(cfdi_id)
    empresa = Empresa.query.get_or_404(cf.empresa_id)
    if empresa.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('dashboard.index'))

    if not cf.xml_content:
        flash('XML no disponible para este CFDI.', 'error')
        return redirect(url_for('dashboard.ver_empresa', empresa_id=cf.empresa_id))

    filename = f'{cf.uuid}.xml'
    return Response(
        cf.xml_content.encode('utf-8'),
        mimetype='application/xml',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@sat_bp.route('/sat/descargar-pdf/<int:cfdi_id>')
@login_required
def descargar_pdf(cfdi_id):
    from flask import Response
    import json as _json
    cf = CFDI.query.get_or_404(cfdi_id)
    empresa = Empresa.query.get_or_404(cf.empresa_id)
    if empresa.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('dashboard.index'))

    if cf.pdf_path:
        upload_dir = os.path.join(current_app.config.get('UPLOAD_FOLDER_CFDIS', 'app/uploads/cfdis'), str(cf.empresa_id))
        pdf_full = os.path.join(upload_dir, cf.pdf_path)
        if os.path.exists(pdf_full):
            with open(pdf_full, 'rb') as f:
                pdf_bytes = f.read()
            return Response(
                pdf_bytes,
                mimetype='application/pdf',
                headers={'Content-Disposition': f'attachment; filename="{cf.uuid}.pdf"'}
            )

    if not cf.xml_content:
        flash('XML no disponible, no se puede generar PDF.', 'error')
        return redirect(url_for('dashboard.ver_empresa', empresa_id=cf.empresa_id))

    try:
        from satcfdi.cfdi import CFDI as SatCFDI
        from satcfdi.render import pdf_bytes as sat_pdf_bytes
        sat_cfdi = SatCFDI.from_string(cf.xml_content.encode('utf-8'))
        pdf = sat_pdf_bytes(sat_cfdi)

        upload_dir = os.path.join(current_app.config.get('UPLOAD_FOLDER_CFDIS', 'app/uploads/cfdis'), str(cf.empresa_id))
        os.makedirs(upload_dir, exist_ok=True)
        pdf_file = os.path.join(upload_dir, f'{cf.uuid}.pdf')
        with open(pdf_file, 'wb') as f:
            f.write(pdf)

        cf.pdf_path = f'{cf.uuid}.pdf'
        db.session.commit()

        return Response(
            pdf,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename="{cf.uuid}.pdf"'}
        )
    except Exception as e:
        flash(f'Error al generar PDF: {str(e)}', 'error')
        return redirect(url_for('dashboard.ver_empresa', empresa_id=cf.empresa_id))


@sat_bp.route('/sat/descarga-masiva', methods=['POST'])
@login_required
def descarga_masiva():
    empresa_id = request.form.get('empresa_id', type=int)
    formato = request.form.get('formato', 'xml')
    cfdis_ids = request.form.getlist('cfdis')

    if not empresa_id or not cfdis_ids:
        flash('Seleccione al menos un CFDI.', 'error')
        return redirect(url_for('dashboard.ver_empresa', empresa_id=empresa_id or 0))

    empresa = Empresa.query.get_or_404(empresa_id)
    if empresa.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('dashboard.index'))

    cfdis = CFDI.query.filter(
        CFDI.id.in_([int(x) for x in cfdis_ids]),
        CFDI.empresa_id == empresa_id
    ).all()

    if not cfdis:
        flash('No se encontraron CFDIs seleccionados.', 'error')
        return redirect(url_for('dashboard.ver_empresa', empresa_id=empresa_id))

    include_xml = formato in ('xml', 'ambos')
    include_pdf = formato in ('pdf', 'ambos')

    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for cf in cfdis:
            if include_xml and cf.xml_content:
                zf.writestr(f'{cf.uuid}.xml', cf.xml_content.encode('utf-8'))
                added += 1

            if include_pdf:
                if cf.pdf_path:
                    upload_dir = os.path.join(
                        current_app.config.get('UPLOAD_FOLDER_CFDIS', 'app/uploads/cfdis'),
                        str(cf.empresa_id))
                    pdf_full = os.path.join(upload_dir, cf.pdf_path)
                    if os.path.exists(pdf_full):
                        zf.write(pdf_full, f'{cf.uuid}.pdf')
                        added += 1
                elif cf.xml_content:
                    try:
                        from satcfdi.cfdi import CFDI as SatCFDI
                        from satcfdi.render import pdf_bytes as sat_pdf_bytes
                        sat_cfdi = SatCFDI.from_string(cf.xml_content.encode('utf-8'))
                        pdf = sat_pdf_bytes(sat_cfdi)
                        zf.writestr(f'{cf.uuid}.pdf', pdf)

                        upload_dir = os.path.join(
                            current_app.config.get('UPLOAD_FOLDER_CFDIS', 'app/uploads/cfdis'),
                            str(cf.empresa_id))
                        os.makedirs(upload_dir, exist_ok=True)
                        pdf_file = os.path.join(upload_dir, f'{cf.uuid}.pdf')
                        with open(pdf_file, 'wb') as f:
                            f.write(pdf)
                        cf.pdf_path = f'{cf.uuid}.pdf'
                        added += 1
                    except Exception:
                        pass

    if added == 0:
        flash('No hay archivos disponibles para los CFDIs seleccionados.', 'error')
        return redirect(url_for('dashboard.ver_empresa', empresa_id=empresa_id))

    db.session.commit()
    buf.seek(0)

    tipo_label = {'xml': 'XML', 'pdf': 'PDF', 'ambos': 'XML_PDF'}.get(formato, 'XML')
    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    filename = f'CFDIS_{empresa.rfc}_{ts}_{tipo_label}.zip'

    return Response(
        buf.getvalue(),
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )
