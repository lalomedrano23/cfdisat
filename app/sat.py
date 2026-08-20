import os
import time
import base64
import io
import zipfile
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, current_app, Response
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
        iva_trasladado = 0
        isr_retenido = 0
        iva_retenido = 0
        if impuestos is not None:
            total_impuestos = float(impuestos.get('TotalImpuestosTrasladados', 0)) + \
                            float(impuestos.get('TotalImpuestosRetenidos', 0))

            traslados = impuestos.find(f'{{{nsmap}}}Traslados') if nsmap else impuestos.find('Traslados')
            if traslados is not None:
                for t in (traslados.findall(f'{{{nsmap}}}Traslado') if nsmap else traslados.findall('Traslado')):
                    imp_code = t.get('Impuesto', '')
                    importe = float(t.get('Importe', 0))
                    if imp_code == '002':
                        iva_trasladado += importe

            retenciones = impuestos.find(f'{{{nsmap}}}Retenciones') if nsmap else impuestos.find('Retenciones')
            if retenciones is not None:
                for r in (retenciones.findall(f'{{{nsmap}}}Retencion') if nsmap else retenciones.findall('Retencion')):
                    imp_code = r.get('Impuesto', '')
                    importe = float(r.get('Importe', 0))
                    if imp_code == '001':
                        isr_retenido += importe
                    elif imp_code == '002':
                        iva_retenido += importe

        conceptos = []
        conceptos_elem = root.find(f'{{{nsmap}}}Conceptos') if nsmap else root.find('Conceptos')
        if conceptos_elem is not None:
            for concepto in (conceptos_elem.findall(f'{{{nsmap}}}Concepto') if nsmap else conceptos_elem.findall('Concepto')):
                conceptos.append({
                    'claveProdServ': concepto.get('ClaveProdServ', ''),
                    'cantidad': float(concepto.get('Cantidad', 0)),
                    'claveUnidad': concepto.get('ClaveUnidad', ''),
                    'descripcion': concepto.get('Descripcion', ''),
                    'valorUnitario': float(concepto.get('ValorUnitario', 0)),
                    'importe': float(concepto.get('Importe', 0)),
                })

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
            'iva_trasladado': iva_trasladado,
            'isr_retenido': isr_retenido,
            'iva_retenido': iva_retenido,
            'estado': estado,
            'uso_cfdi': uso_cfdi,
            'metodo_pago': metodo_pago,
            'forma_pago': forma_pago,
            'serie': serie,
            'folio': folio,
            'moneda': moneda,
            'tipo_cambio': tipo_cambio,
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
                            upload_dir = os.path.join(current_app.config.get('UPLOAD_FOLDER_CFDIS', 'app/uploads/cfdis'), str(empresa.id))
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

            msg = f'Descarga completada: {count} CFDIs sincronizados.'
            if pdf_count:
                msg += f' {pdf_count} PDFs generados.'
            flash(msg, 'success')

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
        dl.mensaje = 'Cancelada por el usuario'
        db.session.commit()
        flash('Descarga cancelada.', 'success')
    else:
        flash('Solo se pueden cancelar descargas en proceso.', 'error')

    return redirect(url_for('dashboard.ver_empresa', empresa_id=dl.empresa_id))


@sat_bp.route('/sat/reprocesar-descarga/<int:request_id>', methods=['POST'])
@login_required
def reprocesar_descarga(request_id):
    dl = DownloadRequest.query.get_or_404(request_id)
    empresa = Empresa.query.get_or_404(dl.empresa_id)
    if empresa.user_id != current_user.id:
        flash('No tienes acceso.', 'error')
        return redirect(url_for('dashboard.index'))

    if dl.estado not in ('procesando', 'error', 'cancelado'):
        flash('No se puede reprocesar esta descarga.', 'error')
        return redirect(url_for('dashboard.ver_empresa', empresa_id=dl.empresa_id))

    dl.estado = 'procesando'
    dl.mensaje = None
    dl.total_descargados = 0
    dl.completed_at = None
    db.session.commit()

    return redirect(url_for('sat.descargar_cfdi'))


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
