import logging
from datetime import datetime, timedelta

from src.database_setup import get_engine
from Tables.Orders_Clean import upsert_orders
from src.order_pull import fetch_all_bulk_orders
from src.utils import load_locations

# Logging is configured by main.py — no setup needed here


def run_order_update(target_date=None, engine=None, API=None):
    """
    Daily pull: fetches the previous day's orders from Toast and upserts them
    into the full-capture schema (orders_head, order_delivery_info,
    order_checks, check_payments, check_discounts, order_items,
    item_applied_taxes, item_discounts, item_modifiers).

    Args:
        target_date (str | None): Business date in YYYYMMDD format.
                                  Defaults to yesterday when None.
        engine: SQLAlchemy engine. Created from get_engine() if not provided.
        API:    Toast API client. Created from ToastAPI() if not provided.
    """
    if target_date is None:
        target_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    if engine is None:
        engine = get_engine()

    if API is None:
        from src.toast_api import ToastAPI
        API = ToastAPI()

    logging.info(f"--- STARTING DAILY SYNC FOR BUSINESS DATE: {target_date} ---")

    try:
        locations = load_locations(engine)
    except Exception as e:
        logging.error(f"CRITICAL: Could not load locations: {e}")
        return

    global_stats = {
        "orders_processed": 0,
        "orders_skipped":   0,
        "items_added":      0,
        "payments_added":   0,
        "locations_ok":     0,
        "locations_failed": 0,
    }

    try:
        with engine.begin() as conn:
            for entry in locations:
                loc_name = entry.get('location_name', 'Unnamed Store')
                loc_guid = entry.get('store_guid')

                if not loc_guid:
                    logging.warning(f"Skipping {loc_name}: no store_guid.")
                    continue

                logging.info(f"[{loc_name}] Fetching orders for {target_date}...")

                try:
                    orders_data = fetch_all_bulk_orders(
                        business_date=target_date,
                        location_id=loc_guid,
                        API=API,
                    )

                    if not orders_data:
                        logging.info(f"[{loc_name}] No orders found for {target_date}.")
                        continue

                    change_log = upsert_orders(conn, orders_data)

                    global_stats["orders_processed"] += change_log.get("orders_processed", 0)
                    global_stats["orders_skipped"]   += change_log.get("orders_skipped",   0)
                    global_stats["items_added"]       += change_log.get("items_added",      0)
                    global_stats["payments_added"]    += change_log.get("payments_added",   0)
                    global_stats["locations_ok"]      += 1

                    logging.info(
                        f"[{loc_name}] OK — "
                        f"{len(orders_data)} orders pulled, "
                        f"{change_log.get('orders_processed', 0)} upserted, "
                        f"{change_log.get('orders_skipped', 0)} unchanged/skipped, "
                        f"{change_log.get('items_added', 0)} items, "
                        f"{change_log.get('payments_added', 0)} payments."
                    )

                except Exception as e:
                    global_stats["locations_failed"] += 1
                    logging.error(f"[{loc_name}] SYNC FAILED: {e}", exc_info=True)

    except Exception as e:
        logging.error(f"CRITICAL DATABASE ERROR: {e}", exc_info=True)
        return

    logging.info("--- DAILY SYNC COMPLETE ---")
    logging.info(
        f"Locations: {global_stats['locations_ok']} ok / "
        f"{global_stats['locations_failed']} failed  |  "
        f"Orders upserted: {global_stats['orders_processed']}  |  "
        f"Orders skipped (unchanged): {global_stats['orders_skipped']}  |  "
        f"Items: {global_stats['items_added']}  |  "
        f"Payments: {global_stats['payments_added']}"
    )


def run_future_order_update(days_ahead=14, engine=None, API=None):
    """
    Future pull: fetches orders for today through +days_ahead to capture
    catering and pre-booked orders entered in Toast with future business dates.
    Staleness checking in upsert_orders ensures only new or changed orders
    result in DB writes — unchanged future orders are skipped cheaply.

    Args:
        days_ahead (int): How many days forward to pull. Default 14.
        engine:          SQLAlchemy engine.
        API:             Toast API client.
    """
    if engine is None:
        engine = get_engine()

    if API is None:
        from src.toast_api import ToastAPI
        API = ToastAPI()

    today = datetime.now().date()
    dates = [
        (today + timedelta(days=i)).strftime("%Y%m%d")
        for i in range(days_ahead + 1)   # today through today+14 inclusive
    ]

    logging.info(f"--- STARTING FUTURE SYNC: {dates[0]} through {dates[-1]} ---")

    try:
        locations = load_locations(engine)
    except Exception as e:
        logging.error(f"CRITICAL: Could not load locations for future sync: {e}")
        return

    global_stats = {
        "orders_processed": 0,
        "orders_skipped":   0,
        "items_added":      0,
        "payments_added":   0,
        "dates_with_orders": 0,
        "locations_failed": 0,
    }

    try:
        with engine.begin() as conn:
            for entry in locations:
                loc_name = entry.get('location_name', 'Unnamed Store')
                loc_guid = entry.get('store_guid')

                if not loc_guid:
                    logging.warning(f"Skipping {loc_name}: no store_guid.")
                    continue

                for target_date in dates:
                    try:
                        orders_data = fetch_all_bulk_orders(
                            business_date=target_date,
                            location_id=loc_guid,
                            API=API,
                        )

                        if not orders_data:
                            continue

                        change_log = upsert_orders(conn, orders_data)

                        global_stats["orders_processed"] += change_log.get("orders_processed", 0)
                        global_stats["orders_skipped"]   += change_log.get("orders_skipped",   0)
                        global_stats["items_added"]       += change_log.get("items_added",      0)
                        global_stats["payments_added"]    += change_log.get("payments_added",   0)

                        if change_log.get("orders_processed", 0) > 0:
                            global_stats["dates_with_orders"] += 1
                            logging.info(
                                f"[{loc_name}] {target_date} — "
                                f"{change_log.get('orders_processed', 0)} upserted, "
                                f"{change_log.get('orders_skipped', 0)} skipped."
                            )

                    except Exception as e:
                        global_stats["locations_failed"] += 1
                        logging.error(f"[{loc_name}] {target_date} FAILED: {e}", exc_info=True)

    except Exception as e:
        logging.error(f"CRITICAL DATABASE ERROR (future sync): {e}", exc_info=True)
        return

    logging.info("--- FUTURE SYNC COMPLETE ---")
    logging.info(
        f"Orders upserted: {global_stats['orders_processed']}  |  "
        f"Orders skipped (unchanged): {global_stats['orders_skipped']}  |  "
        f"Dates with new/changed orders: {global_stats['dates_with_orders']}  |  "
        f"Items: {global_stats['items_added']}  |  "
        f"Payments: {global_stats['payments_added']}"
    )


if __name__ == "__main__":
    run_order_update()
