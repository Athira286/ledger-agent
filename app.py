
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


@lru_cache(maxsize=1)
def load_ledger():
    if not LEDGER_URL:
        raise RuntimeError("Set the LEDGER_URL environment variable.")

    session = requests.Session()

    root = session.get(LEDGER_URL, timeout=TIMEOUT)
    root.raise_for_status()
    api = root.json()

    rates_response = session.get(api["links"]["rates"], timeout=TIMEOUT)
    rates_response.raise_for_status()
    rates = rates_response.json()["usd_per_unit"]

    url = api["links"]["orders"]
    latest = {}

    while url:
        response = session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        page = response.json()

        for order in page.get("orders", []):
            oid = order["id"]
            old = latest.get(oid)

            if old is None or order["updated_at"] > old["updated_at"]:
                latest[oid] = order

        url = page.get("next")

    return list(latest.values()), rates


def date_of(order):
    dt = datetime.fromisoformat(order["created_at"])

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    return dt.astimezone(TZ)


def usd(order, rates):
    return (
        float(order.get("amount", 0))
        * float(rates.get(order.get("currency", "USD"), 1))
    )


def answer(question):
    orders, rates = load_ledger()
    q = question.lower()

    region = next(
        (
            r for r in ["north", "south", "east", "west", "central"]
            if re.search(rf"\b{r}\b", q)
        ),
        None,
    )

    product = next(
        (
            p for p in sorted(
                {o.get("product", "") for o in orders},
                key=len,
                reverse=True,
            )
            if p and p.lower() in q
        ),
        None,
    )

    customer = re.search(r"\bc\d{4}\b", q, re.I)

    month_match = re.search(
        r"\b(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)\s+(20\d{2})\b",
        q,
    )

    month_nums = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }

    ym = (
        (int(month_match.group(2)), month_nums[month_match.group(1)])
        if month_match
        else None
    )

    filtered = []

    for o in orders:
        d = date_of(o)

        if region and o.get("region", "").lower() != region:
            continue

        if product and o.get("product", "").lower() != product.lower():
            continue

        if customer and o.get("customer", "").lower() != customer.group().lower():
            continue

        if ym and (d.year, d.month) != ym:
            continue

        filtered.append(o)

    if not filtered:
        return "No matching orders were found."

    currency_match = re.search(r"\b(usd|eur|inr)\b", q)
    currency = currency_match.group(1).upper() if currency_match else "USD"

    def amount(o):
        if currency == "USD":
            return usd(o, rates)

        if o.get("currency") == currency:
            return float(o.get("amount", 0))

        return usd(o, rates) / float(rates.get(currency, 1))

    # Count distinct customers who have at least one paid order.
    if (
        ("different customers" in q or "distinct customers" in q)
        and "paid" in q
    ):
        rows = [
            o for o in filtered
            if o.get("status") == "paid"
        ]

        customers = {
            o.get("customer")
            for o in rows
            if o.get("customer")
        }

        return f"Different customers: {len(customers)}."

    # Refunds
    if any(w in q for w in ["refund", "refunded"]):
        rows = [
            o for o in filtered
            if o.get("status") == "refunded"
        ]

        total = sum(amount(o) for o in rows)

        return (
            f"Refunded orders: {len(rows)}; "
            f"refund amount: {total:,.2f} {currency}."
        )

    # Revenue: only paid orders count.
    if any(w in q for w in ["revenue", "sales", "earned", "income"]):
        rows = [
            o for o in filtered
            if o.get("status") == "paid"
        ]

        total = sum(amount(o) for o in rows)

        return (
            f"Total revenue: {total:,.2f} {currency} "
            f"from {len(rows)} paid orders."
        )

    # Best-selling product by quantity
    if any(
        w in q
        for w in [
            "top product",
            "best-selling product",
            "most popular product",
        ]
    ):
        totals = {}

        for o in filtered:
            if o.get("status") == "paid":
                p = o.get("product", "Unknown")
                totals[p] = totals.get(p, 0) + int(o.get("qty", 0))

        if not totals:
            return "No paid orders matched."

        p, qty = max(totals.items(), key=lambda x: x[1])

        return f"Best-selling product by quantity: {p} ({qty} units)."

    # Customer-specific details
    if customer:
        rows = [
            o for o in filtered
            if o.get("status") == "paid"
        ]

        return (
            f"Customer {customer.group().upper()}: "
            f"{len(filtered)} matching orders; "
            f"paid revenue {sum(usd(o, rates) for o in rows):,.2f} USD."
        )

    # Generic order count
    if any(w in q for w in ["how many", "number of", "count", "orders"]):
        return f"Matching orders: {len(filtered)}."

    return (
        f"Found {len(filtered)} matching orders. "
        "Ask specifically about revenue, refunds, order count, "
        "or best-selling product."
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

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
