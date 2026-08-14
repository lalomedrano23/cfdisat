import os
import urllib3
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Empresa, FielCredentials, CFDI, DownloadRequest
from app.fiel import decrypt_password

sat_bp = Blueprint('sat', __name__)

# El portal del SAT suele fallar en verificacion SSL (certificado intermedio
# ausente o proxy corporativo). Desactivamos la verificacion solo para el SAT.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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


def _login_sat(signer):
    import ssl
    import requests
    from requests.adapters import HTTPAdapter
    from satcfdi.portal import SATFacturaElectronica

    # Adapter para el SAT: desactiva verificacion SSL y baja el nivel de
    # seguridad de cifrado (el SAT usa parametros DH antiguos -> dh key too small)
    class _InsecureAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.set_ciphers('DEFAULT:@SECLEVEL=0')
            kwargs['ssl_context'] = ctx
            return super().init_poolmanager(*args, **kwargs)

    session = SATFacturaElectronica(signer)
    session.verify = False
    session.mount('https://', _InsecureAdapter())
    session.login()
    return session


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

            session = _login_sat(signer)

            tipo_sat_map = {
                'emitidos': 'CFDI_Emitidos',
                'recibidos': 'CFDI_Recibidos',
                'retenciones_emitidas': 'Retenciones_Emitidas',
                'retenciones_recibidas': 'Retenciones_Recibidas',
            }
            sat_type = tipo_sat_map.get(tipo, 'CFDI_Emitidos')

            url_base = 'https://portalcfdi.facturaelectronica.sat.gob.mx'
            if 'Emitido' in sat_type:
                url_endpoint = f'{url_base}/ReportesCP.aspx'
            elif 'Recibido' in sat_type:
                url_endpoint = f'{url_base}/Reportes.aspx'
            else:
                url_endpoint = f'{url_base}/ReportesCP.aspx'

            params = {
                'rfc': empresa.rfc,
                'fechaInicial': fecha_inicio,
                'fechaFinal': fecha_fin,
                'tipoComprobante': tipo,
                'estadoComprobante': 'Todos',
            }

            resp = session.get(url_endpoint, params=params, timeout=60)

            if resp.status_code != 200:
                raise Exception(f'Error del SAT (HTTP {resp.status_code}): {resp.text[:200]}')

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, 'html.parser')

            cfdis_links = soup.find_all('a', href=True)
            count = 0

            for link in cfdis_links:
                href = link.get('href', '')
                if 'DownloadAttachment' in href or '.zip' in href or 'descarga' in href.lower():
                    try:
                        if href.startswith('/'):
                            href = url_base + href
                        xml_resp = session.get(href, timeout=30)
                        if xml_resp.status_code == 200:
                            metadata = _parse_cfdi_metadata(xml_resp.content, tipo)
                            if metadata and metadata['uuid']:
                                existing = CFDI.query.filter_by(
                                    empresa_id=empresa.id,
                                    uuid=metadata['uuid']
                                ).first()
                                if existing:
                                    existing.estado = metadata.get('estado', 'vigente')
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
                                    xml_content=xml_resp.content.decode('utf-8', errors='ignore'),
                                )
                                db.session.add(cf)
                                count += 1
                    except Exception:
                        continue

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
        from satcfdi.portal import SATFacturaElectronica

        signer, error = _create_signer(empresa)
        if error:
            raise Exception(error)

        session = _login_sat(signer)

        cfdis = CFDI.query.filter_by(empresa_id=empresa.id).all()
        actualizados = 0

        for cf in cfdis:
            try:
                url = f'https://portalcfdi.facturaelectronica.sat.gob.mx/ConsultaCFDIService.svc?uuid={cf.uuid}&rfcEmisor={cf.rfc_emisor}&rfcReceptor={cf.rfc_receptor}&total={cf.total}'
                resp = session.get(url, timeout=10)
                if resp.status_code == 200:
                    if 'Cancelado' in resp.text:
                        if cf.estado != 'cancelado':
                            cf.estado = 'cancelado'
                            actualizados += 1
                    elif 'Vigente' in resp.text or 'cancelado' not in resp.text.lower():
                        if cf.estado != 'vigente':
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
