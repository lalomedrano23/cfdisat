import os
import re
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def get_database_uri():
    # Produccion: PostgreSQL via DATABASE_URL (Render/Neon/Supabase inyectan esta variable)
    database_url = os.getenv('DATABASE_URL')
    if database_url:
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql+psycopg2://', 1)
        # Normaliza cualquier valor de sslmode (incluso incompleto) a 'require'
        database_url = re.sub(r'sslmode=[^&?]+', 'sslmode=require', database_url)
        if 'sslmode=' not in database_url:
            sep = '&' if '?' in database_url else '?'
            database_url = f'{database_url}{sep}sslmode=require'
        return database_url

    connection_name = os.getenv('CLOUD_SQL_CONNECTION_NAME')
    db_user = os.getenv('DB_USER', 'cfdisat')
    db_pass = os.getenv('DB_PASS', '')
    db_name = os.getenv('DB_NAME', 'cfdisat')

    if connection_name:
        return f'mysql+pymysql://{db_user}:{db_pass}@/{db_name}?unix_socket=/cloudsql/{connection_name}'

    # Produccion Render: SQLite en /tmp
    if os.getenv('RENDER'):
        return 'sqlite:////tmp/cfdisat.db'

    # Local: SQLite
    return f'sqlite:///{os.path.join(BASE_DIR, "cfdisat.db")}'


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'cambiame-en-produccion-2026')
    SQLALCHEMY_DATABASE_URI = get_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER_FIEL = os.path.join(BASE_DIR, 'app', 'uploads', 'fiel')
    UPLOAD_FOLDER_CFDIS = os.path.join(BASE_DIR, 'app', 'uploads', 'cfdis')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024