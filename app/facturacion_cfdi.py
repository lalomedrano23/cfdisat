"""Construccion, firma y timbrado de comprobantes CFDI 4.0.

Flujo:
  1. construir_comprobante(...) arma el CFDI 4.0 y lo firma con la FIEL
     (queda en estado "borrador" / pre-timbrado).
  2. timbrar(...) envia el comprobante a un PAC certificado (p. ej. Finkok,
     configurable) o al portal del SAT cuando este disponible, y guarda el
     XML con el TimbreFiscalDigital.

El comprobante se construye con `satcfdi.create.cfd.cfdi40`, la misma
biblioteca que ya se usa en el proyecto.
"""

import logging
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger(__name__)

IVA_TASA = Decimal('0.160000')
BASE_IVA = Decimal('0.160000')


def redondear(valor):
    try:
        return (Decimal(str(valor))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal('0.00')


def _impuestos_concepto(concepto):
    imp = {}
    traslados = []
    retenciones = []
    if concepto.get('aplica_iva', True):
        importe_iva = redondear(concepto.get('iva_importe') or 0)
        traslados.append({
            'impuesto': '002',
            'tipo_factor': 'Tasa',
            'tasa_o_cuota': IVA_TASA,
            'importe': importe_iva,
        })
    if concepto.get('isr_retenido'):
        retenciones.append({
            'impuesto': '001',
            'tipo_factor': 'Tasa',
            'tasa_o_cuota': Decimal('0.100000'),
            'importe': redondear(concepto.get('isr_retenido')),
        })
    if concepto.get('iva_retenido'):
        retenciones.append({
            'impuesto': '002',
            'tipo_factor': 'Tasa',
            'tasa_o_cuota': Decimal('0.106667'),
            'importe': redondear(concepto.get('iva_retenido')),
        })
    if traslados or retenciones:
        imp['traslados'] = traslados or None
        imp['retenciones'] = retenciones or None
    return imp or None


def construir_comprobante(datos, signer):
    """Arma y firma el CFDI 4.0.

    `datos` (dict) campos esperados:
      emisor_rfc, emisor_nombre, emisor_regimen_fiscal,
      lugar_expedicion, receptor_rfc, receptor_nombre, receptor_cp,
      receptor_regimen_fiscal, uso_cfdi, concepto(s), forma_pago,
      metodo_pago, moneda, tipo_cambio, serie, folio, fecha.

    Devuelve (cfdi_obj, xml_bytes) con el comprobante ya firmado.
    """
    from satcfdi.create.cfd.cfdi40 import (
        CFDI, Comprobante, Concepto, Emisor, Impuestos, Receptor,
        Retencion, Traslado,
    )

    emisor = Emisor(
        rfc=datos['emisor_rfc'],
        nombre=datos['emisor_nombre'],
        regimen_fiscal=datos['emisor_regimen_fiscal'],
    )

    receptor = Receptor(
        rfc=datos['receptor_rfc'],
        nombre=datos['receptor_nombre'],
        domicilio_fiscal_receptor=datos['receptor_cp'],
        regimen_fiscal_receptor=datos['receptor_regimen_fiscal'],
        uso_cfdi=datos['uso_cfdi'],
    )

    conceptos = []
    for c in datos['conceptos']:
        impuestos = _impuestos_concepto(c)
        kwargs = {}
        if impuestos:
            kwargs['impuestos'] = Impuestos(
                retenciones=[Retencion(**r) for r in impuestos['retenciones']] if impuestos.get('retenciones') else None,
                traslados=[Traslado(**t) for t in impuestos['traslados']] if impuestos.get('traslados') else None,
            )
        conceptos.append(Concepto(
            clave_prod_serv=c.get('clave_prod_serv', '84111506'),
            cantidad=Decimal(c['cantidad']),
            clave_unidad=c.get('clave_unidad', 'ACT'),
            descripcion=c['descripcion'],
            valor_unitario=redondear(c['valor_unitario']),
            objeto_imp='02' if impuestos else '01',
            **kwargs,
        ))

    comprobante = Comprobante(
        emisor=emisor,
        lugar_expedicion=datos['lugar_expedicion'],
        receptor=receptor,
        conceptos=conceptos,
        serie=datos.get('serie') or None,
        folio=datos.get('folio') or None,
        forma_pago=datos.get('forma_pago') or None,
        metodo_pago=datos.get('metodo_pago') or None,
        moneda=datos.get('moneda') or 'MXN',
        tipo_cambio=Decimal(datos['tipo_cambio']) if datos.get('tipo_cambio') else None,
        fecha=datos.get('fecha'),
    )

    cfdi = comprobante.process()
    cfdi.sign(signer)
    xml = cfdi.xml_bytes(pretty_print=True)
    return cfdi, xml


def timbrar(cfdi, pac_provider=None):
    """Envia el comprobante firmado al PAC y devuelve (uuid, xml_timbrado_bytes).

    `pac_provider` es la cadena configurada (p. ej. 'finkok').
    """
    provider = puerto_pac(pac_provider)
    return provider.timbrar(cfdi)


class _PAC:
    nombre = 'base'

    def timbrar(self, cfdi):
        raise NotImplementedError


class _Finkok(_PAC):
    nombre = 'finkok'

    def timbrar(self, cfdi):
        import os
        from satcfdi.pacs import Environment
        from satcfdi.pacs.finkok import Finkok

        username = os.getenv('FINKOK_USERNAME')
        password = os.getenv('FINKOK_PASSWORD')
        if not username or not password:
            raise RuntimeError('Faltan credenciales FINKOK_USERNAME / FINKOK_PASSWORD '
                               'para el timbrado por PAC.')

        entorno = Environment.TEST
        if os.getenv('FINKOK_ENTORNO', 'test').lower() in ('production', 'produccion', 'prod'):
            entorno = Environment.PRODUCTION

        pac = Finkok(username=username, password=password, environment=entorno)
        doc = pac.issue(cfdi)
        return doc.document_id, doc.xml


class _PortalSAT(_PAC):
    """El SAT opera como PAC; en esta version de satcfdi no esta habilitado
    el canal de timbrado directo (issue/stamp del modulo sat lanzan
    NotImplementedError). Se deja el contrato preparado.
    """
    nombre = 'sat'

    def timbrar(self, cfdi):
        raise NotImplementedError('El timbrado directo via portal SAT no esta '
                                  'habilitado en esta version de satcfdi. '
                                  'Usa un PAC certificado (p. ej. Finkok).')


_PAC_DISPONIBLES = {p.nombre: p for p in (_Finkok(), _PortalSAT())}


def pac_disponibles():
    return list(_PAC_DISPONIBLES.keys())


def puerto_pac(nombre):
    return _PAC_DISPONIBLES.get(nombre or 'finkok', _Finkok())


def firmar_xml_borrador(datos, signer):
    """Comodidad: firma y devuelve el XML firmado (string) con factura borrador."""
    cfdi, xml = construir_comprobante(datos, signer)
    return cfdi, xml.decode('utf-8')