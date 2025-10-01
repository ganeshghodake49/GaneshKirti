from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.cloud import firestore
from datetime import datetime, date
from dateutil import parser
import os
from fastapi import Query
from datetime import date as dt_date
from dateutil import parser
from datetime import date as dt_date
from pydantic import BaseModel
from typing import List, Dict, Any

# Firestore credentials
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", "serviceAccount.json")
db = firestore.Client()

app = FastAPI()

templates = Jinja2Templates(directory="templates")

# --- helper functions ---
def doc_to_row(doc):
    data = doc.to_dict()
    d = data.get("date")
    if hasattr(d, 'date'):
        data["date"] = d.date().isoformat()
    elif isinstance(d, str):
        data["date"] = d
    else:
        data["date"] = date.today().isoformat()
    data["id"] = doc.id
    return data

##########################################################################################################

# --- PRODUCTS ---
@app.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    # fetch products
    docs = db.collection("products").stream()
    products = [doc_to_row(doc) for doc in docs]

    # fetch units (document id or 'name' field if you prefer)
    unit_docs = db.collection("units").stream()
    units = []
    for ud in unit_docs:
        d = ud.to_dict() or {}
        # prefer 'name' field, otherwise doc id
        units.append(d.get("name", ud.id))

    # ensure some defaults exist (optional)
    if not units:
        defaults = ["kg", "ltr", "nos"]
        for u in defaults:
            db.collection("units").document(u).set({"name": u})
        units = defaults

    return templates.TemplateResponse("products.html", {"request": request, "products": products, "units": units})


# Add product: save product and ensure unit is saved to 'units' collection
@app.post("/products/add")
async def add_product(name: str = Form(...), unit: str = Form(...), custom_unit: str = Form(None)):
    name = name.strip()
    # choose unit (custom if selected)
    chosen_unit = unit
    if unit == "custom":
        chosen_unit = (custom_unit or "").strip()

    if name:
        # save product document
        db.collection("products").document(name).set({"name": name, "unit": chosen_unit})

        # also ensure the unit exists in units collection
        if chosen_unit:
            # use unit string as document id for simplicity
            db.collection("units").document(chosen_unit).set({"name": chosen_unit})

    return RedirectResponse("/products", status_code=303)

##########################################################################################################

# --- INVENTORY ---
PAGE_SIZE_DEFAULT = 50  # same as sales

def _doc_to_inventory_dict(doc):
    """Normalize Firestore inventory document into dict with proper types and date parsing"""
    d = doc.to_dict() or {}
    d["id"] = doc.id
    d["quantity"] = float(d.get("quantity", 0) or 0)
    d["price"] = float(d.get("price", 0) or 0)
    d["total"] = float(d.get("total", d["quantity"] * d["price"]) or 0)
    raw_date = d.get("date")
    try:
        if isinstance(raw_date, datetime):
            date_dt = raw_date
        else:
            date_dt = parser.parse(str(raw_date))
        if date_dt.tzinfo:
            date_dt = date_dt.astimezone().replace(tzinfo=None)
        d["date_dt"] = date_dt
        d["date_iso"] = date_dt.isoformat()
        d["date"] = date_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        d["date_dt"] = None
        d["date_iso"] = None
        d["date"] = ""
    d["product"] = d.get("product", "")
    d["unit"] = d.get("unit", "")
    d["party"] = d.get("party", "")
    return d


def _apply_inventory_filters_list(inv_list, start_dt, end_dt, product=None, party=None):
    """Filter a list of inventory dicts already normalized by _doc_to_inventory_dict"""
    out = []
    for i in inv_list:
        dt = i.get("date_dt")
        if not dt:
            continue
        if not (start_dt <= dt <= end_dt):
            continue
        if product and product != "All" and i.get("product") != product:
            continue
        if party and party.strip() and party.lower() not in i.get("party","").lower():
            continue
        out.append(i)
    return out


@app.get("/inventory", response_class=HTMLResponse)
async def inventory_page(
    request: Request,
    start_datetime: str = None,
    end_datetime: str = None,
    product: str = None,
    party: str = None,
    tab: str = "inventory",
    page_size: int = PAGE_SIZE_DEFAULT
):
    now = datetime.now()
    if start_datetime:
        start_dt = parser.parse(start_datetime).replace(tzinfo=None)
    else:
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_datetime = start_dt.strftime("%Y-%m-%dT%H:%M")
    if end_datetime:
        end_dt = parser.parse(end_datetime).replace(tzinfo=None)
    else:
        end_dt = now.replace(hour=23, minute=59, second=0, microsecond=0)
        end_datetime = end_dt.strftime("%Y-%m-%dT%H:%M")

    # Fetch initial page
    coll = db.collection("inventory").order_by("date", direction=firestore.Query.DESCENDING).limit(page_size)
    docs = coll.stream()
    initial = [_doc_to_inventory_dict(doc) for doc in docs]

    # Apply filters
    if tab == "filter":
        initial_filtered = _apply_inventory_filters_list(initial, start_dt, end_dt, product=product, party=party)
    else:
        # new inventory tab always shows today's inventory
        initial_filtered = _apply_inventory_filters_list(initial, start_dt, end_dt)

    has_more = len(initial) >= page_size
    today = now.strftime("%Y-%m-%dT%H:%M")
    products = [p.to_dict() for p in db.collection("products").stream()]

    return templates.TemplateResponse("inventory.html", {
        "request": request,
        "inventory": initial_filtered,
        "products": products,
        "start_datetime": start_datetime,
        "end_datetime": end_datetime,
        "product_filter": product or "All",
        "party_filter": party or "",
        "today": today,
        "active_tab": tab,
        "page_size": page_size,
        "has_more_initial": has_more
    })


@app.get("/inventory/data")
async def inventory_data(
    start_datetime: str = None,
    end_datetime: str = None,
    last_date_iso: str = None,
    limit: int = PAGE_SIZE_DEFAULT,
    product: str = None,
    party: str = None
):
    coll = db.collection("inventory").order_by("date", direction=firestore.Query.DESCENDING)
    try:
        if last_date_iso:
            docs_iter = coll.start_after({"date": last_date_iso}).limit(limit).stream()
        else:
            docs_iter = coll.limit(limit).stream()
    except Exception:
        docs_iter = coll.stream()

    now = datetime.now()
    start_dt = parser.parse(start_datetime).replace(tzinfo=None) if start_datetime else now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = parser.parse(end_datetime).replace(tzinfo=None) if end_datetime else now.replace(hour=23, minute=59, second=0, microsecond=0)

    results = []
    count = 0
    for doc in docs_iter:
        d = _doc_to_inventory_dict(doc)
        if not d["date_dt"]:
            continue
        if not (start_dt <= d["date_dt"] <= end_dt):
            continue
        if product and product != "All" and d.get("product") != product:
            continue
        if party and party.strip() and party.lower() not in d.get("party","").lower():
            continue
        results.append({
            "id": d["id"],
            "date": d["date"],
            "date_iso": d["date_iso"],
            "product": d["product"],
            "quantity": d["quantity"],
            "unit": d["unit"],
            "price": d["price"],
            "total": d["total"],
            "party": d["party"]
        })
        count += 1
        if count >= limit:
            break

    next_cursor = results[-1]["date_iso"] if results else None
    has_more = len(results) >= limit
    return JSONResponse({"inventory": results, "next_cursor": next_cursor, "has_more": has_more})


@app.post("/inventory/add")
async def add_inventory(
    date: str = Form(...),
    product: str = Form(...),
    unit: str = Form(...),
    quantity: float = Form(...),
    price: float = Form(...),
    party: str = Form(None)
):
    try:
        dt_obj = parser.parse(date)
        if dt_obj.tzinfo:
            dt_obj = dt_obj.astimezone().replace(tzinfo=None)
    except Exception:
        dt_obj = datetime.utcnow()

    total = float(quantity) * float(price)
    db.collection("inventory").add({
        "date": dt_obj.isoformat(),
        "product": product,
        "unit": unit,
        "quantity": float(quantity),
        "price": float(price),
        "total": total,
        "party": party or ""
    })
    return RedirectResponse("/inventory", status_code=303)

##########################################################################################################

# --- SALES ---
# --- SALES HELPERS ---
def _doc_to_sale_dict(doc):
    """Normalize Firestore sale document into dict with proper types and date parsing"""
    d = doc.to_dict() or {}
    d["id"] = doc.id
    d["quantity"] = float(d.get("quantity", 0) or 0)
    d["price"] = float(d.get("price", 0) or 0)
    d["total"] = float(d.get("total", d["quantity"] * d["price"]) or 0)
    raw_date = d.get("date")
    try:
        if isinstance(raw_date, datetime):
            date_dt = raw_date
        else:
            date_dt = parser.parse(str(raw_date))
        if date_dt.tzinfo:
            date_dt = date_dt.astimezone().replace(tzinfo=None)
        d["date_dt"] = date_dt
        d["date_iso"] = date_dt.isoformat()
        d["date"] = date_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        d["date_dt"] = None
        d["date_iso"] = None
        d["date"] = ""
    d["product"] = d.get("product", "")
    d["unit"] = d.get("unit", "")
    return d


def _apply_sales_filters_list(sales_list, start_dt, end_dt, product=None):
    """Filter a list of sales dicts already normalized by _doc_to_sale_dict"""
    out = []
    for s in sales_list:
        dt = s.get("date_dt")
        if not dt:
            continue
        if not (start_dt <= dt <= end_dt):
            continue
        if product and product != "All" and s.get("product") != product:
            continue
        out.append(s)
    return out

PAGE_SIZE_DEFAULT = 50  # same as orders

@app.get("/sales", response_class=HTMLResponse)
async def sales_page(
    request: Request,
    start_datetime: str = None,
    end_datetime: str = None,
    product: str = None,
    tab: str = "sales",
    page_size: int = PAGE_SIZE_DEFAULT
):
    now = datetime.now()
    if start_datetime:
        start_dt = parser.parse(start_datetime).replace(tzinfo=None)
    else:
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_datetime = start_dt.strftime("%Y-%m-%dT%H:%M")
    if end_datetime:
        end_dt = parser.parse(end_datetime).replace(tzinfo=None)
    else:
        end_dt = now.replace(hour=23, minute=59, second=0, microsecond=0)
        end_datetime = end_dt.strftime("%Y-%m-%dT%H:%M")

    # Fetch initial page
    coll = db.collection("sales").order_by("date", direction=firestore.Query.DESCENDING).limit(page_size)
    docs = coll.stream()
    initial = [_doc_to_sale_dict(doc) for doc in docs]

    # Apply filters
    initial_filtered = _apply_sales_filters_list(initial, start_dt, end_dt, product=product)

    has_more = len(initial) >= page_size
    today = now.strftime("%Y-%m-%dT%H:%M")
    products = [p.to_dict() for p in db.collection("products").stream()]

    return templates.TemplateResponse("sales.html", {
        "request": request,
        "sales": initial_filtered,
        "products": products,
        "start_datetime": start_datetime,
        "end_datetime": end_datetime,
        "product_filter": product or "All",
        "today": today,
        "active_tab": tab,
        "page_size": page_size,
        "has_more_initial": has_more
    })

@app.get("/sales/data")
async def sales_data(
    start_datetime: str = None,
    end_datetime: str = None,
    last_date_iso: str = None,
    limit: int = PAGE_SIZE_DEFAULT,
    product: str = None
):
    coll = db.collection("sales").order_by("date", direction=firestore.Query.DESCENDING)
    try:
        if last_date_iso:
            docs_iter = coll.start_after({"date": last_date_iso}).limit(limit).stream()
        else:
            docs_iter = coll.limit(limit).stream()
    except Exception:
        docs_iter = coll.stream()

    now = datetime.now()
    start_dt = parser.parse(start_datetime).replace(tzinfo=None) if start_datetime else now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = parser.parse(end_datetime).replace(tzinfo=None) if end_datetime else now.replace(hour=23, minute=59, second=0, microsecond=0)

    results = []
    count = 0
    for doc in docs_iter:
        d = _doc_to_sale_dict(doc)
        if not d["date_dt"]:
            continue
        if not (start_dt <= d["date_dt"] <= end_dt):
            continue
        if product and product != "All" and d.get("product") != product:
            continue
        results.append({
            "id": d["id"],
            "date": d["date"],
            "date_iso": d["date_iso"],
            "product": d["product"],
            "quantity": d["quantity"],
            "unit": d["unit"],
            "price": d["price"],
            "total": d["total"]
        })
        count += 1
        if count >= limit:
            break

    next_cursor = results[-1]["date_iso"] if results else None
    has_more = len(results) >= limit
    return JSONResponse({"sales": results, "next_cursor": next_cursor, "has_more": has_more})

@app.post("/sales/add")
async def add_sale(
    date: str = Form(...),
    product: str = Form(...),
    unit: str = Form(...),
    quantity: float = Form(...),
    price: float = Form(...)
):
    try:
        dt_obj = parser.parse(date)
        if dt_obj.tzinfo:
            dt_obj = dt_obj.astimezone().replace(tzinfo=None)
    except Exception:
        dt_obj = datetime.utcnow()

    total = float(quantity) * float(price)
    db.collection("sales").add({
        "date": dt_obj.isoformat(),
        "product": product,
        "unit": unit,
        "quantity": float(quantity),
        "price": float(price),
        "total": total
    })
    return RedirectResponse("/sales", status_code=303)

##########################################################################################################

# --- ORDERS ---
# --------------------
# Helper functions
# ---------- Orders (cursor-based pagination) ----------
PAGE_SIZE_DEFAULT = 50

def _doc_to_order_dict(doc):
    d = doc.to_dict() or {}
    d["id"] = doc.id
    d["quantity"] = float(d.get("quantity", 0) or 0)
    d["price"] = float(d.get("price", 0) or 0)
    d["total"] = float(d.get("total", d["quantity"] * d["price"]) or 0)
    d["advance"] = float(d.get("advance", 0) or 0)
    d["paid_amount"] = float(d.get("paid_amount", 0) or 0)
    d["remain_amount"] = float(d.get("remain_amount", d["total"] - d["advance"] - d["paid_amount"]) or 0)
    raw_date = d.get("date")
    # parse date to datetime
    try:
        if isinstance(raw_date, datetime):
            date_dt = raw_date
        else:
            date_dt = parser.parse(str(raw_date))
        if date_dt.tzinfo:
            date_dt = date_dt.astimezone().replace(tzinfo=None)
        d["date_dt"] = date_dt
        d["date_iso"] = date_dt.isoformat()
        d["date"] = date_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        d["date_dt"] = None
        d["date_iso"] = None
        d["date"] = ""
    d["status"] = d.get("status", "Pending")
    d["product"] = d.get("product", "")
    d["unit"] = d.get("unit", "")
    d["party"] = d.get("party", "")
    return d

def _apply_filters_list(orders_list, start_dt, end_dt, product=None, party=None, status=None):
    """Filter a list of order dicts already normalized by _doc_to_order_dict."""
    out = []
    for o in orders_list:
        dt = o.get("date_dt")
        if not dt:
            continue
        if not (start_dt <= dt <= end_dt):
            continue
        if product and product != "All" and o.get("product") != product:
            continue
        if status and status != "All" and o.get("status") != status:
            continue
        if party:
            # case-insensitive substring match
            if not o.get("party") or party.lower() not in str(o.get("party")).lower():
                continue
        out.append(o)
    return out

@app.get("/orders", response_class=HTMLResponse)
async def orders_page(
    request: Request,
    start_datetime: str = None,
    end_datetime: str = None,
    product: str = None,
    party: str = None,
    status: str = None,
    tab: str = "new",
    page_size: int = PAGE_SIZE_DEFAULT
):
    """
    Render initial page: first page_size orders matching filters (sorted desc by date).
    Query params:
      - start_datetime, end_datetime (YYYY-MM-DDTHH:MM)
      - product (exact match or 'All')
      - party (substring, case-insensitive)
      - status (exact or 'All')
    """
    now = datetime.now()
    # parse defaults for start/end
    if start_datetime:
        start_dt = parser.parse(start_datetime).replace(tzinfo=None)
    else:
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_datetime = start_dt.strftime("%Y-%m-%dT%H:%M")
    if end_datetime:
        end_dt = parser.parse(end_datetime).replace(tzinfo=None)
    else:
        end_dt = now.replace(hour=23, minute=59, second=0, microsecond=0)
        end_datetime = end_dt.strftime("%Y-%m-%dT%H:%M")

    # Query Firestore for initial page (cursor-based approach)
    coll = db.collection("orders").order_by("date", direction=firestore.Query.DESCENDING).limit(page_size)
    docs = coll.stream()

    initial = []
    for doc in docs:
        d = _doc_to_order_dict(doc)
        initial.append(d)

    # apply filters on the fetched page
    initial_filtered = _apply_filters_list(initial, start_dt, end_dt, product=product, party=party, status=status)

    # We cannot be 100% sure if there are more matching records later without an extra query;
    # set has_more_initial = True if likely more (heuristic: page returned full page_size)
    has_more = False
    if len(initial) == page_size:
        has_more = True

    today = now.strftime("%Y-%m-%dT%H:%M")
    # fetch product list for filter options
    products = [p.to_dict() for p in db.collection("products").stream()]

    return templates.TemplateResponse("orders.html", {
        "request": request,
        "orders": initial_filtered,
        "products": products,
        "start_datetime": start_datetime,
        "end_datetime": end_datetime,
        "product_filter": product or "All",
        "party_filter": party or "",
        "status_filter": status or "All",
        "today": today,
        "active_tab": tab,
        "page_size": page_size,
        "has_more_initial": has_more
    })

@app.get("/orders/data")
async def orders_data(
    start_datetime: str = None,
    end_datetime: str = None,
    last_date_iso: str = None,
    limit: int = PAGE_SIZE_DEFAULT,
    product: str = None,
    party: str = None,
    status: str = None
):
    """
    Return next page slice in JSON. Accepts same filters as /orders and last_date_iso cursor.
    """
    coll = db.collection("orders").order_by("date", direction=firestore.Query.DESCENDING)
    docs_iter = None
    try:
        if last_date_iso:
            docs_iter = coll.start_after({"date": last_date_iso}).limit(limit).stream()
        else:
            docs_iter = coll.limit(limit).stream()
    except Exception:
        docs_iter = coll.stream()

    now = datetime.now()
    if start_datetime:
        start_dt = parser.parse(start_datetime).replace(tzinfo=None)
    else:
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if end_datetime:
        end_dt = parser.parse(end_datetime).replace(tzinfo=None)
    else:
        end_dt = now.replace(hour=23, minute=59, second=0, microsecond=0)

    results = []
    count = 0
    for doc in docs_iter:
        d = _doc_to_order_dict(doc)
        if not d["date_dt"]:
            continue
        # apply filters
        if not (start_dt <= d["date_dt"] <= end_dt):
            continue
        if product and product != "All" and d.get("product") != product:
            continue
        if status and status != "All" and d.get("status") != status:
            continue
        if party:
            if not d.get("party") or party.lower() not in str(d.get("party")).lower():
                continue
        results.append({
            "id": d["id"],
            "date": d["date"],
            "date_iso": d["date_iso"],
            "product": d["product"],
            "quantity": d["quantity"],
            "unit": d["unit"],
            "price": d["price"],
            "total": d["total"],
            "party": d["party"],
            "advance": d["advance"],
            "paid_amount": d["paid_amount"],
            "remain_amount": d["remain_amount"],
            "status": d["status"]
        })
        count += 1
        if count >= limit:
            break

    next_cursor = results[-1]["date_iso"] if results else None
    has_more = len(results) >= limit
    return JSONResponse({"orders": results, "next_cursor": next_cursor, "has_more": has_more})

# Accept form data for adding orders (form submission)
@app.post("/orders/add")
async def add_order(
    date: str = Form(...),
    product: str = Form(...),
    quantity: float = Form(...),
    unit: str = Form(...),
    price: float = Form(...),
    party: str = Form(...),
    advance: float = Form(0.0)
):
    try:
        dt_obj = parser.parse(date)
        if dt_obj.tzinfo:
            dt_obj = dt_obj.astimezone().replace(tzinfo=None)
    except Exception:
        dt_obj = datetime.utcnow()

    total = float(quantity) * float(price)
    paid_amount = 0.0
    remain_amount = total - float(advance) - paid_amount

    doc_ref = db.collection("orders").document()
    doc_ref.set({
        "date": dt_obj.isoformat(),
        "product": product,
        "quantity": float(quantity),
        "unit": unit,
        "price": float(price),
        "total": float(total),
        "party": party,
        "advance": float(advance),
        "paid_amount": float(paid_amount),
        "remain_amount": float(remain_amount),
        "status": "Pending"
    })
    return RedirectResponse(url="/orders?tab=new", status_code=303)

@app.post("/orders/{order_id}/update")
async def update_order(order_id: str, data: dict):
    doc_ref = db.collection("orders").document(order_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Order not found")

    updated_data = {}
    if "status" in data:
        updated_data["status"] = str(data["status"])
    if "paid_amount" in data:
        updated_data["paid_amount"] = float(data["paid_amount"])
    if "remain_amount" in data:
        updated_data["remain_amount"] = float(data["remain_amount"])
    if "advance" in data:
        updated_data["advance"] = float(data["advance"])

    if updated_data:
        doc_ref.update(updated_data)
    return JSONResponse({"success": True})

# --- REPORTS ---
def safe_parse_date(d):
    if d is None:
        return None
    try:
        return parser.parse(str(d)).date()
    except Exception:
        return None
    
@app.get("/reports", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    start_date: str = Query(None),  # e.g., '2025-09-01'
    end_date: str = Query(None)
):
    # Load all data
    inv = [doc.to_dict() for doc in db.collection("inventory").stream()]
    sales = [doc.to_dict() for doc in db.collection("sales").stream()]
    orders = [doc.to_dict() for doc in db.collection("orders").stream()]


    # Then in filtering:
    if start_date:
        start_date_obj = parser.parse(start_date).date()
        inv = [i for i in inv if safe_parse_date(i.get("date")) and safe_parse_date(i.get("date")) >= start_date_obj]
        sales = [s for s in sales if safe_parse_date(s.get("date")) and safe_parse_date(s.get("date")) >= start_date_obj]
        orders = [o for o in orders if safe_parse_date(o.get("date")) and safe_parse_date(o.get("date")) >= start_date_obj]

    if end_date:
        end_date_obj = parser.parse(end_date).date()
        inv = [i for i in inv if safe_parse_date(i.get("date")) and safe_parse_date(i.get("date")) <= end_date_obj]
        sales = [s for s in sales if safe_parse_date(s.get("date")) and safe_parse_date(s.get("date")) <= end_date_obj]
        orders = [o for o in orders if safe_parse_date(o.get("date")) and safe_parse_date(o.get("date")) <= end_date_obj]


    # Compute metrics
    inv_total = sum([float(x.get("total", 0) or 0) for x in inv])
    sales_total = sum([float(x.get("total", 0) or 0) for x in sales])
    inv_qty = sum([float(x.get("quantity", 0) or 0) for x in inv])
    orders_qty = sum([float(x.get("quantity", 0) or 0) for x in orders])

    return templates.TemplateResponse("reports.html", {
        "request": request,
        "inv_total": inv_total,
        "sales_total": sales_total,
        "inv_qty": inv_qty,
        "orders_qty": orders_qty,
        "inventory": inv,
        "sales": sales,
        "orders": orders,
        "start_date": start_date,
        "end_date": end_date
    })

