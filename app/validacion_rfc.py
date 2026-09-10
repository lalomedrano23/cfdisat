"""Validacion de RFC / CURP de acreedores contra fuentes oficiales.

Arquitectura de proveedores: cada proveedor consulta una fuente oficial
(SAT, IMSS, etc.) y devuelve un diccionario normalizado. `validar_rfc`
orquesta todos los proveedores y fusiona los resultados.

Fuentes disponibles hoy:
  - PadronArt69B: listado publicado por el SAT (Art. 69-B) -> estatus real.
  - ConstanciaSAT: constancia de situacion fiscal (requiere id_cif del SAT).
  - ConsultaIMSS: reservado; el IMSS no expone API publica de NSS.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

RFC_RE = re.compile(r'^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{2,3}$')
CURP_RE = re.compile(r'^[A-Z]{4}\d{6}[HM][A-Z]{2}[BCDFGHJKLMNPQRSTVWXYZ]{3}[A-Z0-9]\d$')

REGIMENES = {
    '601': 'General de Ley Personas Morales',
    '603': 'Personas Morales con Fines no Lucrativos',
    '605': 'Sueldos y Salarios e Ingresos Asimilados a Salarios',
    '606': 'Arrendamiento',
    '607': 'Regimen de Enajenacion o Adquisicion de Bienes',
    '608': 'Demas ingresos',
    '609': 'Consolidacion',
    '610': 'Residentes en el Extranjero sin Establecimiento Permanente en Mexico',
    '611': 'Ingresos por Dividendos (socios y accionistas)',
    '612': 'Personas Fisicas con Actividades Empresariales y Profesionales',
    '614': 'Ingresos por intereses',
    '615': 'Regimen de los ingresos por obtencion de premios',
    '616': 'Sin obligaciones fiscales',
    '621': 'Incorporacion Fiscal',
    '625': 'Regimen de las Actividades Empresariales con ingresos a traves de Plataformas Tecnologicas',
    '626': 'Regimen Simplificado de Confianza',
    '628': 'Hidrocarburos',
    '629': 'De los regimenes fiscales preferentes y de las empresas multinacionales',
    '630': 'Enajenacion de acciones en bolsa de valores',
}


class ProveedorError(Exception):
    pass


class ProveedorValidacion:
    nombre = 'base'
    descripcion = ''

    def consultar(self, rfc):
        raise NotImplementedError


class PadronArt69B(ProveedorValidacion):
    """Estatus en el listado completo Art. 69-B publicado por el SAT.

    Mueve el CSV oficial publicado por el SAT y lo cachea internamente.
    No requiere credenciales.
    """
    nombre = 'listado_69b'
    descripcion = 'Padron de contribuyentes con operaciones inexistentes (Art. 69-B SAT)'

    def consultar(self, rfc):
        try:
            from satcfdi.pacs.sat import TaxpayerStatus, _get_listado_69b
            listado = _get_listado_69b()
            estatus = listado.get(rfc.upper())
            if estatus:
                return {'lista_69': TaxpayerStatus(estatus).value}
            return {'lista_69': None}
        except Exception as e:
            logger.warning('[VALIDACION] No se pudo obtener listado 69-B: %s', e)
            return {'lista_69': None, 'error': f'No se pudo consultar el listado 69-B: {e}'}


class ConstanciaSAT(ProveedorValidacion):
    """Constancia de Situacion Fiscal consultada via QR publico del SAT.

    Requiere el id_cif que aparece en la constancia (se obtiene del portal
    del SAT). Devuelve razon social / nombre, codigo postal y regimenes.
    """
    nombre = 'constancia_cif'
    descripcion = 'Constancia de Situacion Fiscal (SAT) — requiere id_cif'

    def __init__(self, id_cif=None):
        self.id_cif = id_cif

    def consultar(self, rfc):
        if not self.id_cif:
            return {'error': 'Se requiere el id_cif para consultar la constancia SAT.'}
        try:
            import satcfdi.csf as csf
            data = csf.retrieve(rfc, self.id_cif)
        except Exception as e:
            logger.warning('[VALIDACION] Constancia fallo: %s', e)
            return {'error': f'No se pudo obtener la constancia: {e}'}

        def _buscar(parejas, *claves):
            for k, v in parejas.items():
                kk = k.lower()
                for c in claves:
                    if c in kk:
                        return v
            return None

        regimenes = data.get('Regimenes') or []
        regimen = None
        fecha_alta = None
        if regimenes:
            regimen = regimenes[0].get('RegimenFiscal')
            fecha_alta = regimenes[0].get('Fecha de alta')

        return {
            'razon_social': _buscar(data, 'razon'),
            'nombre': _buscar(data, 'nombre'),
            'codigo_postal_fiscal': _buscar(data, 'codigo postal'),
            'regimen_fiscal': regimen,
            'fecha_alta': fecha_alta,
        }


class ConsultaIMSS(ProveedorValidacion):
    """Reservado: el IMSS no ofrece una API publica de consulta de NSS.

    Mantiene el contrato para cuando exista un canal habilitado (p. ej.
    con credenciales de patron o servicio autorizado).
    """
    nombre = 'imss_nss'
    descripcion = 'Consulta de NSS en IMSS (reservado — sin API publica)'

    def consultar(self, rfc):
        return {'nss': None, 'error': 'El IMSS no expone una API publica de consulta de NSS.'}


def _validar_formato(rfc=None, curp=None):
    errores = []
    if rfc:
        rfc = rfc.upper().strip()
        if not RFC_RE.match(rfc):
            errores.append('El RFC no tiene un formato valido (letras, 6 digitos y homoclave).')
        if len(rfc) not in (10, 12, 13):
            errores.append('Un RFC debe tener 10, 12 o 13 caracteres.')
    if curp:
        curp = curp.upper().strip()
        if not CURP_RE.match(curp):
            errores.append('La CURP no tiene un formato valido (18 caracteres).')
    return rfc, curp, errores


def _persona_moral(rfc):
    if not rfc:
        return False
    # Persona moral: 12 caracteres (3 letras + fecha + homoclave).
    return len(rfc) == 12


def validar_rfc(rfc=None, curp=None, id_cif=None):
    """Ejecuta la cadena de proveedores y devuelve el resultado normalizado."""
    rfc, curp, errores = _validar_formato(rfc, curp)

    resultado = {
        'rfc': rfc,
        'curp': curp,
        'persona_moral': _persona_moral(rfc),
        'razon_social': None,
        'nombre': None,
        'codigo_postal_fiscal': None,
        'regimen_fiscal': None,
        'regimen_fiscal_desc': None,
        'lista_69': None,
        'nss': None,
        'fuente': None,
        'resultado_completo': {},
        'estado': 'ok',
        'mensaje': '',
    }

    if errores:
        resultado['estado'] = 'error'
        resultado['mensaje'] = ' '.join(errores)
        return resultado

    if not rfc:
        resultado['estado'] = 'parcial'
        resultado['mensaje'] = ('Proporciona el RFC del acreedor para consultar '
                                'las fuentes oficiales del SAT; la CURP se guarda '
                                'como referencia.')
        return resultado

    proveedores = [PadronArt69B(), ConstanciaSAT(id_cif=id_cif), ConsultaIMSS()]

    for prov in proveedores:
        try:
            datos = prov.consultar(rfc)
        except Exception as e:
            datos = {'error': str(e)}
            logger.warning('[VALIDACION] Proveedor %s fallo: %s', prov.nombre, e)

        resultado['resultado_completo'][prov.nombre] = datos

        if prov.nombre == 'listado_69b':
            resultado['lista_69'] = datos.get('lista_69')
            if datos.get('error'):
                resultado['estado'] = 'parcial'
                resultado['mensaje'] = (resultado['mensaje'] + ' ' + datos['error']).strip()
        elif prov.nombre == 'constancia_cif':
            if datos.get('error') and not id_cif:
                # Sin id_cif no es error: la constancia queda como dato pendiente.
                continue
            if datos.get('error'):
                resultado['estado'] = 'parcial'
                resultado['mensaje'] = (resultado['mensaje'] + ' ' + datos['error']).strip()
                continue
            resultado['razon_social'] = datos.get('razon_social')
            resultado['nombre'] = datos.get('nombre')
            resultado['codigo_postal_fiscal'] = datos.get('codigo_postal_fiscal')
            resultado['regimen_fiscal'] = datos.get('regimen_fiscal')
            if not resultado['razon_social']:
                resultado['razon_social'] = resultado['nombre']
            resultado['fuente'] = prov.nombre

    if resultado.get('regimen_fiscal'):
        resultado['regimen_fiscal_desc'] = REGIMENES.get(resultado['regimen_fiscal'], '')

    if resultado['lista_69'] is None and 'listado_69b' in resultado['resultado_completo'] \
            and not resultado['resultado_completo']['listado_69b'].get('error'):
        resultado['lista_69'] = 'No localizado en el listado Art. 69-B'

    if not resultado['mensaje']:
        resultado['mensaje'] = 'Consulta completada contra las fuentes disponibles.'

    return resultado


def serializar(resultado):
    """Guarda el dict normalizado en JSON para la columna resultado_completo."""
    try:
        return json.dumps(resultado, ensure_ascii=False, default=str)
    except Exception:
        return None