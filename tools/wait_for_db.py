import os
import time
import sys
import psycopg2

from app.utils import setup_logging, get_logger

# Configure logging using centralized setup so LOG_LEVEL is respected
setup_logging()
logger = get_logger(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')
RETRIES = int(os.environ.get('DB_WAIT_RETRIES', '30'))
DELAY = float(os.environ.get('DB_WAIT_DELAY', '1.0'))

def wait_for_db():
    if not DATABASE_URL:
        logger.info('No DATABASE_URL provided; skipping DB wait.')
        return 0
    attempt = 0
    while attempt < RETRIES:
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=3)
            conn.close()
            logger.info('Database reachable.')
            return 0
        except Exception as e:
            attempt += 1
            logger.info('Waiting for database (%s/%s)... %s', attempt, RETRIES, e)
            time.sleep(DELAY)
    logger.error('Timed out waiting for database.')
    return 1

if __name__ == '__main__':
    sys.exit(wait_for_db())
