"""Initialize a disposable upstream ERPNext installation with synthetic test records."""

import json
import os

import frappe
from frappe.desk.page.setup_wizard.setup_wizard import (
    initialize_system_settings_and_user,
    setup_complete,
)

os.chdir("/home/frappe/frappe-bench/sites")
frappe.init(site="frontend", sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
frappe.set_user("Administrator")
try:
    settings = dict(
        language="en", country="United States", currency="USD", time_zone="America/Denver"
    )
    initialize_system_settings_and_user(settings, {})
    args = {
        **settings,
        "language": "English",
        "lang": "en",
        "timezone": "America/Denver",
        "company_name": "Computer-Use Replay Test Manufacturing",
        "company_abbr": "CURTM",
        "chart_of_accounts": "Standard",
        "fy_start_date": "2026-01-01",
        "fy_end_date": "2026-12-31",
        "domain": "Manufacturing",
        "enable_telemetry": 0,
    }
    if not frappe.is_setup_complete():
        print("SETUP", setup_complete(args), flush=True)
    for i in range(1, 25):
        name = f"CP-CUSTOMER-{i:03}"
        if not frappe.db.exists("Customer", name):
            frappe.get_doc(
                dict(
                    doctype="Customer",
                    customer_name=name,
                    customer_type="Company" if i % 3 else "Individual",
                    customer_group="Commercial" if i % 2 else "Non Profit",
                    territory="United States",
                    default_currency="USD",
                    tax_id=f"SYNTHETIC-TAX-{i:03}",
                )
            ).insert()
    for code, name, rate in [
        ("CP-PUMP-100", "Process pump", 1490),
        ("CP-PUMP-200", "Process pump high capacity", 2890),
        ("CP-VALVE-100", "Control valve", 175),
        ("CP-SERVICE-100", "Inspection service", 95),
    ]:
        if not frappe.db.exists("Item", code):
            frappe.get_doc(
                dict(
                    doctype="Item",
                    item_code=code,
                    item_name=name,
                    item_group="Products",
                    stock_uom="Nos",
                    is_stock_item=0,
                    standard_rate=rate,
                )
            ).insert()
        if not frappe.db.exists(
            "Item Price", {"item_code": code, "price_list": "Standard Selling"}
        ):
            frappe.get_doc(
                dict(
                    doctype="Item Price",
                    item_code=code,
                    price_list="Standard Selling",
                    price_list_rate=rate,
                    currency="USD",
                )
            ).insert()
    frappe.db.commit()
    print(
        json.dumps(
            {
                "seeded": True,
                "customers": frappe.db.count("Customer"),
                "items": frappe.db.count("Item"),
                "quotations": frappe.db.count("Quotation"),
            }
        ),
        flush=True,
    )
finally:
    frappe.destroy()
