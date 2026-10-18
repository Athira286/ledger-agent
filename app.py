
import os
import re
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import lru_cache
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent")

LEDGER_URL = os.getenv("LEDGER_URL", "").strip()
TZ = ZoneInfo("Asia/Kolkata")
TIMEOUT = 30


class Question(BaseModel):
    question: str


def parse_datetime(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


@lru_cache(maxsize=1)
def load_ledger():
    if not LEDGER_URL:
        raise RuntimeError("Set the LEDGER_URL environment variable.")

    session = requests.Session()

    root_response = session.get(LEDGER_URL, timeout=TIMEOUT)
    root_response.raise_for_status()
    api = root_response.json()
    print("LEDGER LINKS:", api.get("links"))

    rates_response = session.get(api["links"]["rates"], timeout=TIMEOUT)
    rates_response.raise_for_status()
    rates = rates_response.json()["usd_per_unit"]

    latest = {}
    url = api["links"]["orders"]

    while url:
        response = session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        page = response.json()

        for order in page.get("orders", []):
            oid = order["id"]
            old = latest.get(oid)

            if old is None:
                latest[oid] = order
            else:
                if parse_datetime(order["updated_at"]) > parse_datetime(
                    old["updated_at"]
                ):
                    latest[oid] = order

        url = page.get("next")

    return list(latest.values()), rates


def date_of(order):
    return parse_datetime(order["created_at"]).astimezone(TZ)


def usd(order, rates):
    currency = order.get("currency", "USD").upper()
    value = float(order.get("amount", 0))

    # Rates are USD per one unit of the listed currency.
    return value * float(rates.get(currency, 1))


def answer(question):
    orders, rates = load_ledger()
    q = question.lower()

    # Identify optional filters.
    region = next(
        (
            r for r in ["north", "south", "east", "west", "central"]
            if re.search(rf"\b{r}\b", q)
        ),
        None,
    )

    products = sorted(
        {o.get("product", "") for o in orders if o.get("product")},
        key=len,
        reverse=True,
    )
    product = next((p for p in products if p.lower() in q), None)

    customer_match = re.search(r"\bc\d{4}\b", q, re.I)

    month_match = re.search(
        r"\b(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)\s+(20\d{2})\b",
        q,
    )
    month_nums = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    year_month = (
        (int(month_match.group(2)), month_nums[month_match.group(1)])
        if month_match else None
    )

    filtered = []

    for order in orders:
        dt = date_of(order)

        if region and order.get("region", "").lower() != region:
            continue

        if product and order.get("product", "").lower() != product.lower():
            continue

        if customer_match and order.get("customer", "").lower() != (
            customer_match.group().lower()
        ):
            continue

        if year_month and (dt.year, dt.month) != year_month:
            continue

        filtered.append(order)

    if not filtered:
        return "No matching orders were found."

    paid = [o for o in filtered if o.get("status", "").lower() == "paid"]
    refunded = [
        o for o in filtered if o.get("status", "").lower() == "refunded"
    ]

    currency_match = re.search(r"\b(usd|eur|inr)\b", q)
    currency = currency_match.group(1).upper() if currency_match else "USD"

    def converted_amount(order):
        value = float(order.get("amount", 0))
        original_currency = order.get("currency", "USD").upper()

        if original_currency == currency:
            return value

        usd_value = usd(order, rates)
        return usd_value / float(rates.get(currency, 1))

    # Distinct customers with paid orders.
    if (
        "customer" in q
        and any(word in q for word in ["distinct", "different", "unique"])
    ):
        customers = {
            o.get("customer")
            for o in paid
            if o.get("customer")
        }
        return str(len(customers))

    # Top-selling product by revenue must be checked before general revenue.
    revenue_ranking = (
        "top-selling product" in q
        or "top selling product" in q
        or "best-selling product by revenue" in q
        or "best selling product by revenue" in q
        or "highest revenue product" in q
        or "product by revenue" in q
        or "most revenue" in q
        or ("top" in q and "product" in q and "revenue" in q)
    )

    if revenue_ranking:
        totals = {}

        for order in paid:
            name = order.get("product", "Unknown")
            totals[name] = totals.get(name, 0) + usd(order, rates)

        if not totals:
            return "No paid orders matched."

        best_product = max(totals, key=totals.get)
        return best_product

    # Top-selling product by quantity.
    quantity_ranking = (
        "best-selling product" in q
        or "best selling product" in q
        or "most popular product" in q
        or "top product" in q
        or "most units" in q
    )

    if quantity_ranking:
        totals = {}

        for order in paid:
            name = order.get("product", "Unknown")
            totals[name] = totals.get(name, 0) + int(order.get("qty", 0))

        if not totals:
            return "No paid orders matched."

        best_product = max(totals, key=totals.get)
        return best_product

    # Refund totals.
    if "refund" in q or "refunded" in q:
        return str(len(refunded))

    # General revenue: only paid orders count.
    if any(word in q for word in [
        "revenue", "sales", "earned", "income"
    ]):
        total = sum(converted_amount(o) for o in paid)

        return f"{total:.2f}"

    # Customer-specific summary.
    if customer_match:
        customer_id = customer_match.group().upper()
        total = sum(usd(o, rates) for o in paid)

        return (
            f"Customer {customer_id}: {len(filtered)} matching orders; "
            f"paid revenue {total:.2f} USD."
        )

    # Order counts.
    if any(word in q for word in ["how many", "number of", "count", "orders"]):
        return str(len(filtered))

    return (
        f"Found {len(filtered)} matching orders. "
        "Ask about revenue, refunds, customer counts, or best-selling products."
    )


@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "Acme Appliances order ledger",
    }


@app.post("/")
def ask(data: Question):
    try:
        q = data.question.strip()

        if not q:
            raise HTTPException(
                status_code=400,
                detail="Question cannot be empty.",
            )

        return {"answer": answer(q)}

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
