# -*- coding: utf-8 -*-
from sqlalchemy import text
from dateutil.parser import parse as parse_dt
import logging

logger = logging.getLogger("Orders_Clean")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_stored_modified_dates(conn, guids):
    """
    Returns {order_guid: modified_date} for all guids already in orders_head.
    Single query regardless of batch size.
    """
    if not guids:
        return {}
    result = conn.execute(
        text("SELECT order_guid, modified_date FROM orders_head WHERE order_guid = ANY(:guids)"),
        {"guids": list(guids)}
    )
    return {row.order_guid: row.modified_date for row in result}


def _parse_modified(value):
    """Safely parse a Toast ISO-8601 timestamp to a tz-aware datetime."""
    if not value:
        return None
    try:
        dt = parse_dt(value)
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _guid(obj):
    """Safely extract .guid from a nested Toast reference object."""
    if isinstance(obj, dict):
        return obj.get('guid')
    return None


def _disc_fields(disc):
    """Extract all discount fields from a Toast appliedDiscount entry."""
    disc_ref = disc.get('discount') if isinstance(disc.get('discount'), dict) else {}
    approver  = disc.get('approver') if isinstance(disc.get('approver'), dict) else {}
    return {
        "discount_amount":          disc.get('discountAmount'),
        "non_tax_discount_amount":  disc.get('nonTaxDiscountAmount'),
        "discount_name":            disc.get('name') or disc_ref.get('name'),
        "discount_type":            disc.get('discountType') or disc_ref.get('discountType'),
        "discount_percent":         disc.get('discountPercent'),
        "discount_ref_guid":        _guid(disc_ref),
        "processing_state":         disc.get('processingState'),
        "applied_promo_code":       disc.get('appliedPromoCode'),
        "approver_guid":            _guid(approver),
        "external_id":              disc.get('externalId'),
    }


def _refund_fields(obj):
    """Extract refundDetails block from a selection or modifier."""
    ref = obj.get('refundDetails') if isinstance(obj.get('refundDetails'), dict) else {}
    txn = ref.get('refundTransaction') if isinstance(ref.get('refundTransaction'), dict) else {}
    return {
        "refund_amount":            ref.get('refundAmount'),
        "tax_refund_amount":        ref.get('taxRefundAmount'),
        "refund_transaction_guid":  _guid(txn),
    }


# ── Main upsert ───────────────────────────────────────────────────────────────

def upsert_orders(conn, orders_list):
    """
    Full-capture upsert aligned with the complete Toast API JSON schema.
    Orders whose modifiedDate is not newer than the stored modified_date
    are skipped — no unnecessary writes.

    Tier structure:
        1   orders_head             – order header
        1b  order_delivery_info     – delivery address (one-to-one)
        2   order_checks            – checks on the order
        2b  check_payments          – payments on each check
        2c  check_discounts         – check-level discounts
        2d  check_service_charges   – service charges / gratuity
        2e  service_charge_taxes    – taxes on service charges
        3   order_items             – item selections
        3b  item_applied_taxes      – per-item tax breakdown
        3c  item_discounts          – selection-level discounts
        4   item_modifiers          – modifiers on each selection
    """
    stats = {
        "orders_processed":  0,
        "orders_skipped":    0,
        "items_added":       0,
        "payments_added":    0,
    }

    if not orders_list:
        return stats

    # ── Staleness gate ────────────────────────────────────────────────────────
    incoming_guids = {
        o['guid']: _parse_modified(o.get('modifiedDate'))
        for o in orders_list
        if isinstance(o, dict) and o.get('guid')
    }
    stored_dates = _fetch_stored_modified_dates(conn, set(incoming_guids.keys()))

    def is_stale(o_guid, incoming_modified):
        stored = stored_dates.get(o_guid)
        if stored is None:
            return False
        if incoming_modified is None:
            return False
        if stored.tzinfo is None:
            from datetime import timezone
            stored = stored.replace(tzinfo=timezone.utc)
        return incoming_modified <= stored

    # ── Accumulators ─────────────────────────────────────────────────────────
    tier1_heads           = []
    tier1b_delivery       = []
    tier2_checks          = []
    tier2b_payments       = []
    tier2c_chk_discounts  = []
    tier2d_svc_charges    = []
    tier2e_svc_taxes      = []
    tier3_items           = []
    tier3b_taxes          = []
    tier3c_item_discounts = []
    tier4_mods            = []

    # ── Extraction & Transformation ───────────────────────────────────────────
    for order in orders_list:
        if not isinstance(order, dict) or not order.get('guid'):
            continue

        o_guid = order['guid']

        if is_stale(o_guid, incoming_guids.get(o_guid)):
            stats["orders_skipped"] += 1
            continue

        # Top-level reference objects
        d_opt    = order.get('diningOption')    or {}
        table_ob = order.get('table')           or {}
        s_area   = order.get('serviceArea')     or {}   # order-level serviceArea
        service  = order.get('restaurantService') or {}
        rev_ctr  = order.get('revenueCenter')   or {}
        server   = order.get('server')          or {}
        c_dev    = order.get('createdDevice')   or {}
        lm_dev   = order.get('lastModifiedDevice') or {}
        curbside = order.get('curbsidePickupInfo') or {}

        # ── TIER 1: Order Header ──────────────────────────────────────────────
        tier1_heads.append({
            "order_guid":                   o_guid,
            "location_id":                  order.get('location_id'),
            "external_id":                  order.get('externalId'),
            "order_number":                 order.get('displayNumber'),

            "fire_date":                    order.get('openedDate'),
            "promised_date":                order.get('promisedDate'),
            "created_date":                 order.get('createdDate'),
            "closed_date":                  order.get('closedDate'),
            "paid_date":                    order.get('paidDate'),
            "modified_date":                order.get('modifiedDate'),
            "deleted_date":                 order.get('deletedDate'),
            "void_date":                    order.get('voidDate'),
            "void_business_date":           order.get('voidBusinessDate'),
            "estimated_fulfillment_date":   order.get('estimatedFulfillmentDate'),
            "business_date":                order.get('businessDate'),

            "required_prep_time":           order.get('requiredPrepTime'),
            "number_of_guests":             order.get('numberOfGuests'),
            "approval_status":              order.get('approvalStatus'),
            "source":                       order.get('source'),
            "pricing_features":             order.get('pricingFeatures'),
            "channel_guid":                 order.get('channelGuid'),
            "duration":                     order.get('duration'),

            "deleted":                      order.get('deleted', False),
            "voided":                       order.get('voided', False),
            "excess_food":                  order.get('excessFood', False),
            "created_in_test_mode":         order.get('createdInTestMode', False),

            "dining_option_guid":           _guid(d_opt),
            "service_area_guid":            _guid(s_area),
            "table_guid":                   _guid(table_ob),
            "restaurant_service_guid":      _guid(service),
            "revenue_center_guid":          _guid(rev_ctr),
            "server_guid":                  _guid(server),

            "curbside_guid":                curbside.get('guid'),
            "curbside_transport_color":     curbside.get('transportColor'),
            "curbside_transport_desc":      curbside.get('transportDescription'),
            "curbside_notes":               curbside.get('notes'),

            "created_device_id":            c_dev.get('id'),
            "last_modified_device_id":      lm_dev.get('id'),
        })

        # ── TIER 1b: Delivery Info ────────────────────────────────────────────
        delivery = order.get('deliveryInfo')
        if delivery and isinstance(delivery, dict):
            tier1b_delivery.append({
                "order_guid":               o_guid,
                "address1":                 delivery.get('address1'),
                "address2":                 delivery.get('address2'),
                "city":                     delivery.get('city'),
                "state":                    delivery.get('state'),
                "zip_code":                 delivery.get('zipCode'),
                "country":                  delivery.get('country'),
                "administrative_area":      delivery.get('administrativeArea'),
                "latitude":                 delivery.get('latitude'),
                "longitude":                delivery.get('longitude'),
                "notes":                    delivery.get('notes'),
                "delivery_state":           delivery.get('deliveryState'),
                "dispatched_date":          delivery.get('dispatchedDate'),
                "delivered_date":           delivery.get('deliveredDate'),
                "delivery_employee_guid":   _guid(delivery.get('deliveryEmployee')),
            })

        # ── TIER 2: Checks ────────────────────────────────────────────────────
        for check in [c for c in order.get('checks', []) if isinstance(c, dict) and c.get('guid')]:
            c_guid   = check['guid']
            cust     = check.get('customer')          or {}
            loyalty  = check.get('appliedLoyaltyInfo') or {}
            c_dev_c  = check.get('createdDevice')     or {}
            lm_dev_c = check.get('lastModifiedDevice') or {}
            opened_by = check.get('openedBy')         or {}

            tier2_checks.append({
                "check_guid":               c_guid,
                "order_guid":               o_guid,
                "external_id":              check.get('externalId'),
                "display_number":           check.get('displayNumber'),

                "payment_status":           check.get('paymentStatus'),
                "tax_exempt":               check.get('taxExempt', False),
                "tax_exemption_account":    check.get('taxExemptionAccount'),
                "total_amount":             check.get('totalAmount'),
                "tax_amount":               check.get('taxAmount'),
                "net_amount":               check.get('amount'),
                "tab_name":                 check.get('tabName'),

                "customer_guid":            cust.get('guid'),
                "customer_first":           cust.get('firstName'),
                "customer_last":            cust.get('lastName'),
                "customer_phone":           cust.get('phone'),
                "customer_phone_country":   cust.get('phoneCountryCode'),
                "customer_email":           cust.get('email'),

                "loyalty_guid":             loyalty.get('guid'),
                "loyalty_identifier":       loyalty.get('loyaltyIdentifier'),
                "loyalty_vendor":           loyalty.get('vendor'),

                "opened_date":              check.get('openedDate'),
                "closed_date":              check.get('closedDate'),
                "paid_date":                check.get('paidDate'),
                "void_date":                check.get('voidDate'),
                "void_business_date":       check.get('voidBusinessDate'),
                "created_date":             check.get('createdDate'),
                "modified_date":            check.get('modifiedDate'),
                "deleted_date":             check.get('deletedDate'),
                "duration":                 check.get('duration'),

                "voided":                   check.get('voided', False),
                "deleted":                  check.get('deleted', False),

                "opened_by_guid":           _guid(opened_by),
                "created_device_id":        c_dev_c.get('id'),
                "last_modified_device_id":  lm_dev_c.get('id'),
            })

            # ── TIER 2b: Payments ─────────────────────────────────────────────
            for pmt in [p for p in check.get('payments', []) if isinstance(p, dict) and p.get('guid')]:
                lm_dev_p  = pmt.get('lastModifiedDevice') or {}
                c_dev_p   = pmt.get('createdDevice')      or {}
                refund    = pmt.get('refund')              or {}
                refund_tx = _guid(refund.get('refundTransaction') or {})
                void_info = pmt.get('voidInfo')            or {}
                void_user = _guid(void_info.get('voidUser')     or {})
                void_appr = _guid(void_info.get('voidApprover') or {})
                void_rsn  = _guid(void_info.get('voidReason')   or {})

                tier2b_payments.append({
                    "payment_guid":             pmt['guid'],
                    "check_guid":               c_guid,
                    "order_guid":               o_guid,
                    "external_id":              pmt.get('externalId'),

                    "type":                     pmt.get('type'),
                    "payment_status":           pmt.get('paymentStatus'),
                    "amount":                   pmt.get('amount'),
                    "tip_amount":               pmt.get('tipAmount'),
                    "amount_tendered":          pmt.get('amountTendered'),
                    "original_processing_fee":  pmt.get('originalProcessingFee'),
                    "surcharge_amount":         pmt.get('surchargeAmount'),
                    "mca_repayment_amount":     pmt.get('mcaRepaymentAmount'),
                    "refund_status":            pmt.get('refundStatus'),

                    "card_type":                pmt.get('cardType'),
                    "card_entry_mode":          pmt.get('cardEntryMode'),
                    "last_4_digits":            pmt.get('last4Digits'),
                    "first_6_digits":           pmt.get('first6Digits'),
                    "card_processor_type":      pmt.get('cardProcessorType'),
                    "card_payment_id":          pmt.get('cardPaymentId'),

                    "refund_amount":            refund.get('refundAmount'),
                    "tip_refund_amount":        refund.get('tipRefundAmount'),
                    "refund_date":              refund.get('refundDate'),
                    "refund_business_date":     refund.get('refundBusinessDate'),
                    "refund_transaction_guid":  refund_tx,

                    "void_date":                void_info.get('voidDate'),
                    "void_business_date":       void_info.get('voidBusinessDate'),
                    "void_reason_guid":         void_rsn,
                    "void_user_guid":           void_user,
                    "void_approver_guid":       void_appr,

                    "server_guid":              _guid(pmt.get('server')),
                    "cash_drawer_guid":         _guid(pmt.get('cashDrawer')),
                    "house_account_guid":       _guid(pmt.get('houseAccount')),
                    "other_payment_guid":       _guid(pmt.get('otherPayment')),

                    "paid_date":                pmt.get('paidDate'),
                    "paid_business_date":       pmt.get('paidBusinessDate'),
                    "is_processed_offline":     pmt.get('isProcessedOffline', False),

                    "created_device_id":        c_dev_p.get('id'),
                    "last_modified_device_id":  lm_dev_p.get('id'),
                    "tender_transaction_guid":  pmt.get('tenderTransactionGuid'),
                    "network_transaction_id":   pmt.get('networkTransactionIdentifier'),
                })
                stats["payments_added"] += 1

            # ── TIER 2c: Check-level Discounts ────────────────────────────────
            for disc in [d for d in check.get('appliedDiscounts', []) if isinstance(d, dict) and d.get('guid')]:
                tier2c_chk_discounts.append({
                    "discount_guid": disc['guid'],
                    "check_guid":    c_guid,
                    **_disc_fields(disc),
                })

            # ── TIER 2d: Service Charges ──────────────────────────────────────
            for chg in [c for c in check.get('appliedServiceCharges', []) if isinstance(c, dict) and c.get('guid')]:
                chg_guid  = chg['guid']
                svc_ref   = chg.get('serviceCharge') or {}
                ref_det   = chg.get('refundDetails')  or {}
                ref_tx    = _guid(ref_det.get('refundTransaction') or {})

                tier2d_svc_charges.append({
                    "charge_guid":                  chg_guid,
                    "check_guid":                   c_guid,
                    "external_id":                  chg.get('externalId'),
                    "name":                         chg.get('name'),
                    "charge_amount":                chg.get('chargeAmount'),
                    "charge_type":                  chg.get('chargeType'),
                    "service_charge_guid":          _guid(svc_ref),
                    "service_charge_category":      chg.get('serviceChargeCategory'),
                    "payment_guid":                 chg.get('paymentGuid'),

                    "delivery":                     chg.get('delivery', False),
                    "takeout":                      chg.get('takeout', False),
                    "dine_in":                      chg.get('dineIn', False),
                    "gratuity":                     chg.get('gratuity', False),
                    "taxable":                      chg.get('taxable', False),

                    "service_charge_calculation":   chg.get('serviceChargeCalculation'),

                    "refund_amount":                ref_det.get('refundAmount'),
                    "tax_refund_amount":            ref_det.get('taxRefundAmount'),
                    "refund_transaction_guid":      ref_tx,
                })

                # ── TIER 2e: Service Charge Taxes ─────────────────────────────
                for tax in [t for t in chg.get('appliedTaxes', []) if isinstance(t, dict) and t.get('guid')]:
                    tax_rate_ref = tax.get('taxRate') or {}
                    tier2e_svc_taxes.append({
                        "applied_tax_guid":           tax['guid'],
                        "charge_guid":                chg_guid,
                        "tax_rate_guid":              _guid(tax_rate_ref),
                        "tax_name":                   tax.get('name'),
                        "rate":                       tax.get('rate'),
                        "tax_amount":                 tax.get('taxAmount'),
                        "type":                       tax.get('type'),
                        "jurisdiction_type":          tax.get('jurisdictionType'),
                        "jurisdiction":               tax.get('jurisdiction'),
                        "display_name":               tax.get('displayName'),
                        "facilitator_collect_remit":  tax.get('facilitatorCollectAndRemitTax', False),
                    })

            # ── TIER 3: Item Selections ───────────────────────────────────────
            for sel in [s for s in check.get('selections', []) if isinstance(s, dict) and s.get('guid')]:
                s_guid    = sel['guid']
                item_ref  = sel.get('item')          or {}
                item_grp  = sel.get('itemGroup')     or {}
                opt_grp   = sel.get('optionGroup')   or {}
                pre_mod   = sel.get('preModifier')   or {}
                sales_cat = sel.get('salesCategory') or {}
                din_opt   = sel.get('diningOption')  or {}
                split_org = sel.get('splitOrigin')   or {}
                ref_flds  = _refund_fields(sel)

                tier3_items.append({
                    "selection_guid":               s_guid,
                    "check_guid":                   c_guid,
                    "external_id":                  sel.get('externalId'),

                    "item_guid":                    _guid(item_ref),
                    "item_multi_location_id":       item_ref.get('multiLocationId'),
                    "item_group_guid":              _guid(item_grp),
                    "item_group_multi_loc_id":      item_grp.get('multiLocationId'),
                    "sales_category_guid":          _guid(sales_cat),
                    "option_group_guid":            _guid(opt_grp),
                    "option_group_multi_loc_id":    opt_grp.get('multiLocationId'),
                    "pre_modifier_guid":            _guid(pre_mod),
                    "pre_modifier_multi_loc_id":    pre_mod.get('multiLocationId'),
                    "dining_option_guid":           _guid(din_opt),
                    "split_origin_guid":            _guid(split_org),

                    "item_name":                    sel.get('displayName'),
                    "selection_type":               sel.get('selectionType'),
                    "unit_of_measure":              sel.get('unitOfMeasure'),
                    "plu":                          sel.get('plu'),
                    "premodifier_plu":              sel.get('premodifierPlu'),
                    "sales_category_plu":           sel.get('salesCategoryPlu'),
                    "seat_number":                  sel.get('seatNumber'),

                    "quantity":                     sel.get('quantity'),
                    "unit_price":                   sel.get('receiptLinePrice'),
                    "net_price":                    sel.get('price'),
                    "pre_discount_price":           sel.get('preDiscountPrice'),
                    "open_price_amount":            sel.get('openPriceAmount'),
                    "external_price_amount":        sel.get('externalPriceAmount'),
                    "tax_amount":                   sel.get('tax'),
                    "tax_inclusion":                sel.get('taxInclusion'),
                    "option_group_pricing_mode":    sel.get('optionGroupPricingMode'),

                    "refund_amount":                ref_flds["refund_amount"],
                    "tax_refund_amount":            ref_flds["tax_refund_amount"],
                    "refund_transaction_guid":      ref_flds["refund_transaction_guid"],

                    "fulfillment_status":           sel.get('fulfillmentStatus'),
                    "voided":                       sel.get('voided', False),
                    "deferred":                     sel.get('deferred', False),

                    "created_date":                 sel.get('createdDate'),
                    "modified_date":                sel.get('modifiedDate'),
                    "void_date":                    sel.get('voidDate'),
                    "void_business_date":           sel.get('voidBusinessDate'),
                    "void_reason":                  _guid(sel.get('voidReason')),
                })
                stats["items_added"] += 1

                # ── TIER 3b: Applied Taxes per Selection ──────────────────────
                for tax in [t for t in sel.get('appliedTaxes', []) if isinstance(t, dict) and t.get('guid')]:
                    tax_rate_ref = tax.get('taxRate') or {}
                    tier3b_taxes.append({
                        "applied_tax_guid":           tax['guid'],
                        "selection_guid":             s_guid,
                        "tax_rate_guid":              _guid(tax_rate_ref),
                        "tax_name":                   tax.get('name'),
                        "rate":                       tax.get('rate'),
                        "tax_amount":                 tax.get('taxAmount'),
                        "type":                       tax.get('type'),
                        "jurisdiction_type":          tax.get('jurisdictionType'),
                        "jurisdiction":               tax.get('jurisdiction'),
                        "display_name":               tax.get('displayName'),
                        "facilitator_collect_remit":  tax.get('facilitatorCollectAndRemitTax', False),
                    })

                # ── TIER 3c: Selection-level Discounts ────────────────────────
                for disc in [d for d in sel.get('appliedDiscounts', []) if isinstance(d, dict) and d.get('guid')]:
                    tier3c_item_discounts.append({
                        "discount_guid":  disc['guid'],
                        "selection_guid": s_guid,
                        **_disc_fields(disc),
                    })

                # ── TIER 4: Modifiers ─────────────────────────────────────────
                for mod in [m for m in sel.get('modifiers', []) if isinstance(m, dict) and m.get('guid')]:
                    m_item    = mod.get('item')        or {}
                    m_opt_grp = mod.get('optionGroup') or {}
                    m_pre_mod = mod.get('preModifier') or {}
                    m_din_opt = mod.get('diningOption') or {}
                    m_split   = mod.get('splitOrigin') or {}
                    m_ref     = _refund_fields(mod)

                    tier4_mods.append({
                        "modifier_guid":                mod['guid'],
                        "selection_guid":               s_guid,
                        "external_id":                  mod.get('externalId'),

                        "item_guid":                    _guid(m_item),
                        "item_multi_location_id":       m_item.get('multiLocationId'),
                        "option_group_guid":            _guid(m_opt_grp),
                        "option_group_multi_loc":       m_opt_grp.get('multiLocationId'),
                        "pre_modifier_guid":            _guid(m_pre_mod),
                        "pre_modifier_multi_loc_id":    m_pre_mod.get('multiLocationId'),
                        "dining_option_guid":           _guid(m_din_opt),
                        "split_origin_guid":            _guid(m_split),

                        "mod_name":                     mod.get('displayName'),
                        "selection_type":               mod.get('selectionType'),
                        "unit_of_measure":              mod.get('unitOfMeasure'),
                        "plu":                          mod.get('plu'),
                        "premodifier_plu":              mod.get('premodifierPlu'),
                        "sales_category_plu":           mod.get('salesCategoryPlu'),
                        "seat_number":                  mod.get('seatNumber'),

                        "quantity":                     mod.get('quantity'),
                        "mod_unit_price":               mod.get('receiptLinePrice'),
                        "mod_net_price":                mod.get('price'),
                        "pre_discount_price":           mod.get('preDiscountPrice'),
                        "open_price_amount":            mod.get('openPriceAmount'),
                        "external_price_amount":        mod.get('externalPriceAmount'),
                        "tax_amount":                   mod.get('tax'),
                        "tax_inclusion":                mod.get('taxInclusion'),
                        "option_group_pricing_mode":    mod.get('optionGroupPricingMode'),

                        "refund_amount":                m_ref["refund_amount"],
                        "tax_refund_amount":            m_ref["tax_refund_amount"],
                        "refund_transaction_guid":      m_ref["refund_transaction_guid"],

                        "fulfillment_status":           mod.get('fulfillmentStatus'),
                        "voided":                       mod.get('voided', False),
                        "deferred":                     mod.get('deferred', False),

                        "created_date":                 mod.get('createdDate'),
                        "modified_date":                mod.get('modifiedDate'),
                        "void_date":                    mod.get('voidDate'),
                        "void_business_date":           mod.get('voidBusinessDate'),
                        "void_reason":                  _guid(mod.get('voidReason')),
                    })

        stats["orders_processed"] += 1

    # ── Batch Execution ───────────────────────────────────────────────────────
    try:
        # ── TIER 1 ───────────────────────────────────────────────────────────
        if tier1_heads:
            conn.execute(text("""
                INSERT INTO orders_head (
                    order_guid, location_id, external_id, order_number,
                    fire_date, promised_date, created_date, closed_date, paid_date,
                    modified_date, deleted_date, void_date, void_business_date,
                    estimated_fulfillment_date, business_date,
                    required_prep_time, number_of_guests, approval_status,
                    source, pricing_features, channel_guid, duration,
                    deleted, voided, excess_food, created_in_test_mode,
                    dining_option_guid, service_area_guid, table_guid,
                    restaurant_service_guid, revenue_center_guid, server_guid,
                    curbside_guid, curbside_transport_color,
                    curbside_transport_desc, curbside_notes,
                    created_device_id, last_modified_device_id
                ) VALUES (
                    :order_guid, :location_id, :external_id, :order_number,
                    :fire_date, :promised_date, :created_date, :closed_date, :paid_date,
                    :modified_date, :deleted_date, :void_date, :void_business_date,
                    :estimated_fulfillment_date, :business_date,
                    :required_prep_time, :number_of_guests, :approval_status,
                    :source, :pricing_features, :channel_guid, :duration,
                    :deleted, :voided, :excess_food, :created_in_test_mode,
                    :dining_option_guid, :service_area_guid, :table_guid,
                    :restaurant_service_guid, :revenue_center_guid, :server_guid,
                    :curbside_guid, :curbside_transport_color,
                    :curbside_transport_desc, :curbside_notes,
                    :created_device_id, :last_modified_device_id
                )
                ON CONFLICT (order_guid) DO UPDATE SET
                    deleted                 = EXCLUDED.deleted,
                    voided                  = EXCLUDED.voided,
                    modified_date           = EXCLUDED.modified_date,
                    closed_date             = EXCLUDED.closed_date,
                    paid_date               = EXCLUDED.paid_date,
                    last_modified_device_id = EXCLUDED.last_modified_device_id;
            """), tier1_heads)

        # ── TIER 1b ──────────────────────────────────────────────────────────
        if tier1b_delivery:
            conn.execute(text("""
                INSERT INTO order_delivery_info (
                    order_guid, address1, address2, city, state, zip_code,
                    country, administrative_area, latitude, longitude, notes,
                    delivery_state, dispatched_date, delivered_date,
                    delivery_employee_guid
                ) VALUES (
                    :order_guid, :address1, :address2, :city, :state, :zip_code,
                    :country, :administrative_area, :latitude, :longitude, :notes,
                    :delivery_state, :dispatched_date, :delivered_date,
                    :delivery_employee_guid
                )
                ON CONFLICT (order_guid) DO UPDATE SET
                    delivery_state          = EXCLUDED.delivery_state,
                    dispatched_date         = EXCLUDED.dispatched_date,
                    delivered_date          = EXCLUDED.delivered_date,
                    delivery_employee_guid  = EXCLUDED.delivery_employee_guid;
            """), tier1b_delivery)

        # ── TIER 2 ───────────────────────────────────────────────────────────
        if tier2_checks:
            conn.execute(text("""
                INSERT INTO order_checks (
                    check_guid, order_guid, external_id, display_number,
                    payment_status, tax_exempt, tax_exemption_account,
                    total_amount, tax_amount, net_amount, tab_name,
                    customer_guid, customer_first, customer_last,
                    customer_phone, customer_phone_country, customer_email,
                    loyalty_guid, loyalty_identifier, loyalty_vendor,
                    opened_date, closed_date, paid_date, void_date,
                    void_business_date, created_date, modified_date,
                    deleted_date, duration,
                    voided, deleted,
                    opened_by_guid, created_device_id, last_modified_device_id
                ) VALUES (
                    :check_guid, :order_guid, :external_id, :display_number,
                    :payment_status, :tax_exempt, :tax_exemption_account,
                    :total_amount, :tax_amount, :net_amount, :tab_name,
                    :customer_guid, :customer_first, :customer_last,
                    :customer_phone, :customer_phone_country, :customer_email,
                    :loyalty_guid, :loyalty_identifier, :loyalty_vendor,
                    :opened_date, :closed_date, :paid_date, :void_date,
                    :void_business_date, :created_date, :modified_date,
                    :deleted_date, :duration,
                    :voided, :deleted,
                    :opened_by_guid, :created_device_id, :last_modified_device_id
                )
                ON CONFLICT (check_guid) DO UPDATE SET
                    payment_status  = EXCLUDED.payment_status,
                    total_amount    = EXCLUDED.total_amount,
                    tax_amount      = EXCLUDED.tax_amount,
                    net_amount      = EXCLUDED.net_amount,
                    closed_date     = EXCLUDED.closed_date,
                    paid_date       = EXCLUDED.paid_date,
                    voided          = EXCLUDED.voided,
                    deleted         = EXCLUDED.deleted;
            """), tier2_checks)

        # ── TIER 2b ──────────────────────────────────────────────────────────
        if tier2b_payments:
            conn.execute(text("""
                INSERT INTO check_payments (
                    payment_guid, check_guid, order_guid, external_id,
                    type, payment_status, amount, tip_amount, amount_tendered,
                    original_processing_fee, surcharge_amount, mca_repayment_amount,
                    refund_status, card_type, card_entry_mode,
                    last_4_digits, first_6_digits, card_processor_type, card_payment_id,
                    refund_amount, tip_refund_amount, refund_date,
                    refund_business_date, refund_transaction_guid,
                    void_date, void_business_date, void_reason_guid,
                    void_user_guid, void_approver_guid,
                    server_guid, cash_drawer_guid,
                    house_account_guid, other_payment_guid,
                    paid_date, paid_business_date, is_processed_offline,
                    created_device_id, last_modified_device_id,
                    tender_transaction_guid, network_transaction_id
                ) VALUES (
                    :payment_guid, :check_guid, :order_guid, :external_id,
                    :type, :payment_status, :amount, :tip_amount, :amount_tendered,
                    :original_processing_fee, :surcharge_amount, :mca_repayment_amount,
                    :refund_status, :card_type, :card_entry_mode,
                    :last_4_digits, :first_6_digits, :card_processor_type, :card_payment_id,
                    :refund_amount, :tip_refund_amount, :refund_date,
                    :refund_business_date, :refund_transaction_guid,
                    :void_date, :void_business_date, :void_reason_guid,
                    :void_user_guid, :void_approver_guid,
                    :server_guid, :cash_drawer_guid,
                    :house_account_guid, :other_payment_guid,
                    :paid_date, :paid_business_date, :is_processed_offline,
                    :created_device_id, :last_modified_device_id,
                    :tender_transaction_guid, :network_transaction_id
                )
                ON CONFLICT (payment_guid) DO UPDATE SET
                    payment_status      = EXCLUDED.payment_status,
                    refund_status       = EXCLUDED.refund_status,
                    amount              = EXCLUDED.amount,
                    tip_amount          = EXCLUDED.tip_amount,
                    refund_amount       = EXCLUDED.refund_amount,
                    tip_refund_amount   = EXCLUDED.tip_refund_amount,
                    void_date           = EXCLUDED.void_date,
                    void_reason_guid    = EXCLUDED.void_reason_guid;
            """), tier2b_payments)

        # ── TIER 2c ──────────────────────────────────────────────────────────
        if tier2c_chk_discounts:
            conn.execute(text("""
                INSERT INTO check_discounts (
                    discount_guid, check_guid, external_id,
                    discount_amount, non_tax_discount_amount,
                    discount_name, discount_type, discount_percent,
                    discount_ref_guid, processing_state,
                    applied_promo_code, approver_guid
                ) VALUES (
                    :discount_guid, :check_guid, :external_id,
                    :discount_amount, :non_tax_discount_amount,
                    :discount_name, :discount_type, :discount_percent,
                    :discount_ref_guid, :processing_state,
                    :applied_promo_code, :approver_guid
                )
                ON CONFLICT (discount_guid) DO NOTHING;
            """), tier2c_chk_discounts)

        # ── TIER 2d ──────────────────────────────────────────────────────────
        if tier2d_svc_charges:
            conn.execute(text("""
                INSERT INTO check_service_charges (
                    charge_guid, check_guid, external_id,
                    name, charge_amount, charge_type,
                    service_charge_guid, service_charge_category, payment_guid,
                    delivery, takeout, dine_in, gratuity, taxable,
                    service_charge_calculation,
                    refund_amount, tax_refund_amount, refund_transaction_guid
                ) VALUES (
                    :charge_guid, :check_guid, :external_id,
                    :name, :charge_amount, :charge_type,
                    :service_charge_guid, :service_charge_category, :payment_guid,
                    :delivery, :takeout, :dine_in, :gratuity, :taxable,
                    :service_charge_calculation,
                    :refund_amount, :tax_refund_amount, :refund_transaction_guid
                )
                ON CONFLICT (charge_guid) DO UPDATE SET
                    charge_amount   = EXCLUDED.charge_amount,
                    refund_amount   = EXCLUDED.refund_amount;
            """), tier2d_svc_charges)

        # ── TIER 2e ──────────────────────────────────────────────────────────
        if tier2e_svc_taxes:
            conn.execute(text("""
                INSERT INTO service_charge_taxes (
                    applied_tax_guid, charge_guid, tax_rate_guid,
                    tax_name, rate, tax_amount, type,
                    jurisdiction_type, jurisdiction, display_name,
                    facilitator_collect_remit
                ) VALUES (
                    :applied_tax_guid, :charge_guid, :tax_rate_guid,
                    :tax_name, :rate, :tax_amount, :type,
                    :jurisdiction_type, :jurisdiction, :display_name,
                    :facilitator_collect_remit
                )
                ON CONFLICT (applied_tax_guid) DO NOTHING;
            """), tier2e_svc_taxes)

        # ── TIER 3 ───────────────────────────────────────────────────────────
        if tier3_items:
            conn.execute(text("""
                INSERT INTO order_items (
                    selection_guid, check_guid, external_id,
                    item_guid, item_multi_location_id,
                    item_group_guid, item_group_multi_loc_id,
                    sales_category_guid, option_group_guid, option_group_multi_loc_id,
                    pre_modifier_guid, pre_modifier_multi_loc_id,
                    dining_option_guid, split_origin_guid,
                    item_name, selection_type, unit_of_measure,
                    plu, premodifier_plu, sales_category_plu, seat_number,
                    quantity, unit_price, net_price, pre_discount_price,
                    open_price_amount, external_price_amount,
                    tax_amount, tax_inclusion, option_group_pricing_mode,
                    refund_amount, tax_refund_amount, refund_transaction_guid,
                    fulfillment_status, voided, deferred,
                    created_date, modified_date, void_date,
                    void_business_date, void_reason
                ) VALUES (
                    :selection_guid, :check_guid, :external_id,
                    :item_guid, :item_multi_location_id,
                    :item_group_guid, :item_group_multi_loc_id,
                    :sales_category_guid, :option_group_guid, :option_group_multi_loc_id,
                    :pre_modifier_guid, :pre_modifier_multi_loc_id,
                    :dining_option_guid, :split_origin_guid,
                    :item_name, :selection_type, :unit_of_measure,
                    :plu, :premodifier_plu, :sales_category_plu, :seat_number,
                    :quantity, :unit_price, :net_price, :pre_discount_price,
                    :open_price_amount, :external_price_amount,
                    :tax_amount, :tax_inclusion, :option_group_pricing_mode,
                    :refund_amount, :tax_refund_amount, :refund_transaction_guid,
                    :fulfillment_status, :voided, :deferred,
                    :created_date, :modified_date, :void_date,
                    :void_business_date, :void_reason
                )
                ON CONFLICT (selection_guid) DO UPDATE SET
                    voided                  = EXCLUDED.voided,
                    fulfillment_status      = EXCLUDED.fulfillment_status,
                    modified_date           = EXCLUDED.modified_date,
                    item_group_guid         = EXCLUDED.item_group_guid,
                    item_group_multi_loc_id = EXCLUDED.item_group_multi_loc_id,
                    refund_amount           = EXCLUDED.refund_amount;
            """), tier3_items)

        # ── TIER 3b ──────────────────────────────────────────────────────────
        if tier3b_taxes:
            conn.execute(text("""
                INSERT INTO item_applied_taxes (
                    applied_tax_guid, selection_guid, tax_rate_guid,
                    tax_name, rate, tax_amount, type,
                    jurisdiction_type, jurisdiction, display_name,
                    facilitator_collect_remit
                ) VALUES (
                    :applied_tax_guid, :selection_guid, :tax_rate_guid,
                    :tax_name, :rate, :tax_amount, :type,
                    :jurisdiction_type, :jurisdiction, :display_name,
                    :facilitator_collect_remit
                )
                ON CONFLICT (applied_tax_guid) DO NOTHING;
            """), tier3b_taxes)

        # ── TIER 3c ──────────────────────────────────────────────────────────
        if tier3c_item_discounts:
            conn.execute(text("""
                INSERT INTO item_discounts (
                    discount_guid, selection_guid, external_id,
                    discount_amount, non_tax_discount_amount,
                    discount_name, discount_type, discount_percent,
                    discount_ref_guid, processing_state,
                    applied_promo_code, approver_guid
                ) VALUES (
                    :discount_guid, :selection_guid, :external_id,
                    :discount_amount, :non_tax_discount_amount,
                    :discount_name, :discount_type, :discount_percent,
                    :discount_ref_guid, :processing_state,
                    :applied_promo_code, :approver_guid
                )
                ON CONFLICT (discount_guid) DO NOTHING;
            """), tier3c_item_discounts)

        # ── TIER 4 ───────────────────────────────────────────────────────────
        if tier4_mods:
            conn.execute(text("""
                INSERT INTO item_modifiers (
                    modifier_guid, selection_guid, external_id,
                    item_guid, item_multi_location_id,
                    option_group_guid, option_group_multi_loc,
                    pre_modifier_guid, pre_modifier_multi_loc_id,
                    dining_option_guid, split_origin_guid,
                    mod_name, selection_type, unit_of_measure,
                    plu, premodifier_plu, sales_category_plu, seat_number,
                    quantity, mod_unit_price, mod_net_price, pre_discount_price,
                    open_price_amount, external_price_amount,
                    tax_amount, tax_inclusion, option_group_pricing_mode,
                    refund_amount, tax_refund_amount, refund_transaction_guid,
                    fulfillment_status, voided, deferred,
                    created_date, modified_date, void_date,
                    void_business_date, void_reason
                ) VALUES (
                    :modifier_guid, :selection_guid, :external_id,
                    :item_guid, :item_multi_location_id,
                    :option_group_guid, :option_group_multi_loc,
                    :pre_modifier_guid, :pre_modifier_multi_loc_id,
                    :dining_option_guid, :split_origin_guid,
                    :mod_name, :selection_type, :unit_of_measure,
                    :plu, :premodifier_plu, :sales_category_plu, :seat_number,
                    :quantity, :mod_unit_price, :mod_net_price, :pre_discount_price,
                    :open_price_amount, :external_price_amount,
                    :tax_amount, :tax_inclusion, :option_group_pricing_mode,
                    :refund_amount, :tax_refund_amount, :refund_transaction_guid,
                    :fulfillment_status, :voided, :deferred,
                    :created_date, :modified_date, :void_date,
                    :void_business_date, :void_reason
                )
                ON CONFLICT (modifier_guid) DO UPDATE SET
                    voided          = EXCLUDED.voided,
                    modified_date   = EXCLUDED.modified_date,
                    refund_amount   = EXCLUDED.refund_amount;
            """), tier4_mods)

    except Exception as e:
        logger.error(f"Error during Orders_Clean batch insert: {e}")
        raise

    return stats
