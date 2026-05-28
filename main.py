# -*- coding: utf-8 -*-
import logging
import os
import sys
from datetime import datetime, timedelta

# Local development only — Azure injects env vars directly
if os.path.exists(".env"):
    from dotenv import load_dotenv
    load_dotenv(override=True)

from src.database_setup import get_engine
from src.toast_api import ToastAPI
from Tables.Orders_Pull_Update import run_order_update

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("main_future")


def main():
    start = datetime.now()
    today = start.date()

    logger.info("=" * 60)
    logger.info(f"FUTURE ORDER SYNC STARTED — {start.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # ── Shared resources ──────────────────────────────────────────────────────
    try:
        engine = get_engine()
        logger.info("Database engine initialised.")
    except Exception as e:
        logger.critical(f"Could not create database engine: {e}", exc_info=True)
        sys.exit(1)

    try:
        api = ToastAPI(engine=engine)
        logger.info("Toast API client initialised.")
    except Exception as e:
        logger.critical(f"Could not initialise Toast API client: {e}", exc_info=True)
        sys.exit(1)

    # ── Pull today through today+14 ───────────────────────────────────────────
    for i in range(15):  # 0 through 14 inclusive
        target_date = (today + timedelta(days=i)).strftime("%Y%m%d")
        try:
            run_order_update(target_date=target_date, engine=engine, API=api)
        except Exception as e:
            # Log and continue — one bad date shouldn't stop the rest
            logger.error(f"Sync failed for {target_date}: {e}", exc_info=True)

    # ── Done ──────────────────────────────────────────────────────────────────
    elapsed = datetime.now() - start
    logger.info("=" * 60)
    logger.info(f"FUTURE ORDER SYNC COMPLETE — elapsed {elapsed}")
    logger.info("=" * 60)
    sys.exit(0)


if __name__ == "__main__":
    main()
