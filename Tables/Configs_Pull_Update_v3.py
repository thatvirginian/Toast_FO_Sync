# -*- coding: utf-8 -*-
import logging
import requests
from datetime import datetime
from sqlalchemy import text

from src.toast_api import ToastAPI
from src.utils import load_locations
from src.database_setup import get_engine

logger = logging.getLogger("Configs_Pull_Update")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _flatten_guid_fields(record):
    """
    Walks a raw Toast API record and pulls nested {guid: ...} objects up
    to a flat  <key>_guid  column, matching the config table DDL.
    """
    flat = {}
    guid_promoted = {"revenueCenter", "serviceArea", "businessPeriod"}
    for key, value in record.items():
        if isinstance(value, dict) and "guid" in value:
            flat[f"{key}_guid"] = value.get("guid")
        elif key in guid_promoted:
            flat[f"{key}_guid"] = value
        else:
            flat[key] = value
    return flat


def _pull_endpoint(endpoint, api, locations):
    """
    Calls a single Toast config endpoint for every location, flattens the
    results, and de-duplicates by GUID (shared items appear at every location).
    """
    seen = {}
    for store in locations:
        store_guid = store.get("store_guid")
        if not store_guid:
            continue
        headers = api.get_headers()
        headers["Toast-Restaurant-External-ID"] = store_guid
        try:
            response = requests.get(
                api.base_url + endpoint,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                data = [data]
            for item in data:
                flat = _flatten_guid_fields(item)
                if flat.get("guid"):
                    seen[flat["guid"]] = flat
        except Exception as e:
            logger.error(f"API error on {endpoint} for {store_guid}: {e}")

    return list(seen.values())


# ── Per-table upsert SQL ──────────────────────────────────────────────────────

_UPSERT_SQL = {
    "revenue_centers": text("""
        INSERT INTO revenue_centers (guid, name, description)
        VALUES (:guid, :name, :description)
        ON CONFLICT (guid) DO UPDATE SET
            name        = EXCLUDED.name,
            description = EXCLUDED.description
    """),

    "dining_options": text("""
        INSERT INTO dining_options (guid, name, behavior)
        VALUES (:guid, :name, :behavior)
        ON CONFLICT (guid) DO UPDATE SET
            name     = EXCLUDED.name,
            behavior = EXCLUDED.behavior
    """),

    "services": text("""
        INSERT INTO services (guid, name)
        VALUES (:guid, :name)
        ON CONFLICT (guid) DO UPDATE SET
            name = EXCLUDED.name
    """),

    "employees": text("""
        INSERT INTO employees (guid, first_name, last_name, email, deleted)
        VALUES (:guid, :firstName, :lastName, :email, :deleted)
        ON CONFLICT (guid) DO UPDATE SET
            first_name = EXCLUDED.first_name,
            last_name  = EXCLUDED.last_name,
            email      = EXCLUDED.email,
            deleted    = EXCLUDED.deleted
    """),

    "sales_categories": text("""
        INSERT INTO sales_categories (guid, name)
        VALUES (:guid, :name)
        ON CONFLICT (guid) DO UPDATE SET
            name = EXCLUDED.name
    """),

    "service_areas": text("""
        INSERT INTO service_areas (guid, name, revenue_center_guid)
        VALUES (:guid, :name, :revenueCenter_guid)
        ON CONFLICT (guid) DO UPDATE SET
            name                = EXCLUDED.name,
            revenue_center_guid = EXCLUDED.revenue_center_guid
    """),

    "tables": text("""
        INSERT INTO tables (guid, name, revenue_center_guid, service_area_guid)
        VALUES (:guid, :name, :revenueCenter_guid, :serviceArea_guid)
        ON CONFLICT (guid) DO UPDATE SET
            name                = EXCLUDED.name,
            revenue_center_guid = EXCLUDED.revenue_center_guid,
            service_area_guid   = EXCLUDED.service_area_guid
    """),
}

# Toast API endpoints, ordered so dependencies resolve first
# (revenue_centers before service_areas before tables)
_ENDPOINTS = {
    "revenue_centers":  "/config/v2/revenueCenters",
    "dining_options":   "/config/v2/diningOptions",
    "services":         "/config/v2/restaurantServices",
    "employees":        "/labor/v1/employees",
    "sales_categories": "/config/v2/salesCategories",
    "service_areas":    "/config/v2/serviceAreas",
    "tables":           "/config/v2/tables",
}


# ── Main entry point ──────────────────────────────────────────────────────────

def run_config_sync(engine=None, API=None):
    """
    Pulls all configuration/dimension data from Toast and upserts it into
    the config tables.  Safe to run before the daily orders sync so that
    all foreign-key GUIDs (server, dining option, revenue center, etc.)
    are already resolved in the DB.

    Args:
        engine: SQLAlchemy engine.  Created from get_engine() if not provided.
        API:    ToastAPI client.    Created internally if not provided.
    """
    if engine is None:
        engine = get_engine()
    if API is None:
        API = ToastAPI()

    logger.info(f"{'='*60}")
    logger.info(f"TOAST CONFIG SYNC — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'='*60}")

    locations = load_locations(engine)
    if not locations:
        logger.error("No locations loaded — aborting config sync.")
        return

    global_stats = {}

    with engine.begin() as conn:
        for table_name, endpoint in _ENDPOINTS.items():
            logger.info(f"[{table_name}] Fetching from Toast API...")

            results = _pull_endpoint(endpoint, API, locations)

            if not results:
                logger.info(f"[{table_name}] No data returned.")
                global_stats[table_name] = 0
                continue

            sql = _UPSERT_SQL.get(table_name)
            if sql is None:
                logger.error(f"[{table_name}] No SQL mapping defined — skipping.")
                continue

            try:
                conn.execute(sql, results)
                global_stats[table_name] = len(results)
                logger.info(f"[{table_name}] {len(results)} records upserted.")
            except Exception as e:
                logger.error(f"[{table_name}] DB error: {e}", exc_info=True)
                global_stats[table_name] = -1
                # Raise so engine.begin() rolls back the whole transaction cleanly
                raise

    logger.info("CONFIG SYNC COMPLETE")
    for table, count in global_stats.items():
        status = f"{count} records" if count >= 0 else "FAILED"
        logger.info(f"  {table:<20} {status}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("sync_history.log"),
            logging.StreamHandler(),
        ],
    )
    run_config_sync()
