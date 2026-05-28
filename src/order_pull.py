# -*- coding: utf-8 -*-
import time
import requests
import logging
from src.toast_api import ToastAPI

logger = logging.getLogger("OrderPull")


def fetch_bulk_orders(start_date=None, end_date=None, business_date=None, location_id=None, page=1, page_size=100, retries=3, API=None):
    api = API
    api_host = api.base_url

    if not api_host:
        raise ValueError("Missing TOAST_API_HOST in environment variables.")

    if not location_id:
        raise ValueError("Location ID must be provided.")

    url = f"{api_host}/orders/v2/ordersBulk"
    headers = api.get_headers()
    headers["Toast-Restaurant-External-ID"] = location_id

    # Normalizing business_date to YYYYMMDD
    if business_date:
        business_date = str(business_date).replace("-", "").replace("/", "").replace(" ", "")

    params = {
        "page": str(page),
        "pageSize": str(page_size)
    }

    if business_date:
        params["businessDate"] = business_date
    else:
        params["startDate"] = start_date
        params["endDate"] = end_date

    # Standardizing response handling
    response = requests.get(url, headers=headers, params=params)
    # If the token expired mid-run
    if response.status_code == 401:
        print("Token expired mid-request. Refreshing and retrying...")
        logger.warning("Token expired mid-request. Refreshing and retrying...")
        api._authenticate()  # Force a fresh one
        headers["Authorization"] = f"Bearer {api.token}"
        response = requests.get(url, headers=headers, params=params, timeout=30)

    if response.status_code == 429:
        if retries > 0:
            wait_time = (4 - retries) * 3  # Incremental wait: 5s, 10s, 15s
            print(f"Rate limit hit. Retrying in {wait_time}s... ({retries} left)")
            logger.warning(f"Rate limit hit. Retrying in {wait_time}s... ({retries} left)")
            time.sleep(wait_time)
            # Pass retries - 1 to eventually hit the exit condition
            return fetch_bulk_orders(start_date, end_date, business_date, location_id, page, page_size, retries - 1, API=api)
        else:
            print("Max retries reached for Rate Limiting. Skipping.")
            logger.error("Max retries reached for Rate Limiting. Skipping.")
            response.raise_for_status()


    response.raise_for_status()
    return response.json()


def fetch_all_bulk_orders(start_date=None, end_date=None, business_date=None, location_id=None, API=None):
    # Safety: If main.py didn't provide one, create a local one.
    if API is None:
        from src.toast_api import ToastAPI
        API = ToastAPI()
    
    """
    Paginates through all orders for a specific Anita's location.
    Injects location_id into each order object for the Tiered Upsert logic.
    """
    all_orders = []
    page = 1
    page_size = 100

    while True:
        try:
            batch = fetch_bulk_orders(
                start_date=start_date,
                end_date=end_date,
                business_date=business_date,
                location_id=location_id,
                page=page,
                page_size=page_size,
                API=API
            )
            print(business_date,page)
            if not isinstance(batch, list):
                print(f"Invalid batch format for {location_id} on page {page}")
                logger.error(f"Invalid batch format for {location_id} on page {page}")
                break

            # CRITICAL: Attach location_id so upsert_orders knows which store owns the record
            for order in batch:
                order["location_id"] = location_id

            all_orders.extend(batch)
            print(len(batch),page_size)
            # If we got fewer results than the page size, we've reached the end
            if len(batch) < page_size:
                break

            page += 1
            time.sleep(1)  # Slight delay to be a good API citizen

        except Exception as e:
            print(f"Error fetching page {page} for {location_id}: {e}")
            logger.error(f"Error fetching page {page} for {location_id}: {e}")
            break
    print("Orders RETRIEVED")
    return all_orders