#!/usr/bin/env python3
"""Daily scraper: count active rental listings per DHCR rent-stabilized building.

Sources (Manhattan + Brooklyn only):
  - Zumper search pages
  - RentHop search pages

Listings whose address doesn't match a known DHCR building are dropped.
Output: listings.json — {counts:{bbl:n}, urls:{bbl:url}, prices:{bbl:min_rent}}
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

HERE = Path(__file__).parent
BUILDINGS = HERE / "buildings.min.json"
OUT = HERE / "listings.json"
LOG = HERE / "scrape.log"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

SUFFIX_MAP = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "PLACE": "PL",
    "ROAD": "RD", "DRIVE": "DR", "LANE": "LN", "TERRACE": "TER",
    "COURT": "CT", "PARKWAY": "PKWY", "SQUARE": "SQ", "HEIGHTS": "HTS",
}
DIRECTION_MAP = {"WEST": "W", "EAST": "E", "NORTH": "N", "SOUTH": "S"}
SPECIAL_NAME_MAP = {
    "AVENUE OF THE AMERICAS": "6TH AVE",
    "AVE OF THE AMERICAS": "6TH AVE",
}

ZIP_RE = re.compile(r"\b\d{5}\b")
ADDR_NUM_RE = re.compile(r"^(\d+)(?:\s+|\b)")


def normalize_addr(s: str) -> str:
    """Return canonical 'NUMBER REST' string (e.g. '246 10TH AVE')."""
    if not s:
        return ""
    s = s.upper().strip()
    # strip unit / apt
    s = re.split(r"\s+(?:#|APT|UNIT|SUITE|STE)\b", s)[0].strip()
    s = re.sub(r"[#,.;]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # special multi-word names first
    for k, v in SPECIAL_NAME_MAP.items():
        if k in s:
            s = s.replace(k, v)
    parts = s.split(" ")
    out = []
    for tok in parts:
        if tok in SUFFIX_MAP:
            out.append(SUFFIX_MAP[tok])
        elif tok in DIRECTION_MAP:
            out.append(DIRECTION_MAP[tok])
        else:
            out.append(tok)
    return " ".join(out)


def build_index(records):
    """Return dict normalized_addr -> bbl. For range-numbered DHCR rows, index every number in the range."""
    idx = {}
    for r in records:
        if r["b"] not in ("M", "Bk"):
            continue
        for raw in (r.get("a"), r.get("address_alt")):
            if not raw:
                continue
            norm = normalize_addr(raw)
            if not norm:
                continue
            # handle range like "303 TO 309 10TH AVE"
            m = re.match(r"^(\d+)\s+TO\s+(\d+)\s+(.+)$", norm)
            if m:
                lo, hi, rest = int(m.group(1)), int(m.group(2)), m.group(3)
                step = 2 if (hi - lo) % 2 == 0 else 1
                for n in range(lo, hi + 1, step):
                    idx[f"{n} {rest}"] = r["bbl"]
            else:
                idx[norm] = r["bbl"]
    return idx


async def fetch_page(ctx, url, *, wait_ms=2500, timeout_ms=25000):
    page = await ctx.new_page()
    await stealth_async(page)
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(wait_ms)
        html = await page.content()
        status = resp.status if resp else 0
        return status, html
    except Exception as e:
        return 0, f"ERR: {e}"
    finally:
        await page.close()


async def scrape_zumper(ctx, area_slug: str, max_pages=80, log=print):
    """Return list of (address, url) tuples from Zumper rentals search."""
    seen_addrs = set()
    out = []
    base = f"https://www.zumper.com/apartments-for-rent/{area_slug}"
    for page_n in range(1, max_pages + 1):
        url = base if page_n == 1 else f"{base}?page={page_n}"
        status, html = await fetch_page(ctx, url)
        if status != 200:
            log(f"  zumper {area_slug} p{page_n} status={status}, stopping")
            break
        # Zumper embeds listings in the page as JSON. Each entry has
        # "listing_id":NUMBER ... "address":"..." ... and a price field.
        # Building the URL via /listing/<id> works (Zumper 301-redirects to the canonical building page).
        pairs = []
        # Each listing is a JSON object starting at "listing_id". Bound the window to the
        # next listing_id so the address + price we read belong to this listing. Zumper
        # carries "min_price" ~1.5-3.6k chars into the object (the cheapest unit advertised).
        id_matches = list(re.finditer(r'"listing_id":(\d+)', html))
        for idx, m in enumerate(id_matches):
            lid = m.group(1)
            end = id_matches[idx + 1].start() if idx + 1 < len(id_matches) else m.start() + 6000
            window = html[m.start():end]
            am = re.search(r'"address":"([^"]{5,80})"', window)
            if not am:
                continue
            addr = am.group(1)
            pm = re.search(r'"min_price":(\d{3,7})', window)
            price = int(pm.group(1)) if pm else None
            pairs.append((addr, f"https://www.zumper.com/listing/{lid}", price))
        # Add any bare addresses not already paired (fallback, no url/price)
        bare_addrs = set(re.findall(r'"address":"([^"]{5,80})"', html))
        addrs_with_url = {a for a, _, _ in pairs}
        for a in bare_addrs - addrs_with_url:
            pairs.append((a, None, None))
        new = [(a, u, pr) for a, u, pr in pairs if a not in seen_addrs]
        log(f"  zumper {area_slug} p{page_n}: {len(pairs)} addrs ({len(new)} new, "
            f"{sum(1 for _,u,_ in new if u)} with URL, {sum(1 for _,_,pr in new if pr)} with price)")
        if not new:
            break
        for a, u, pr in new:
            seen_addrs.add(a)
            out.append((a, u, pr))
        await asyncio.sleep(1.2)
    return out


async def scrape_renthop(ctx, area_path: str, max_pages=20, log=print):
    """Return list of (address, url) tuples from RentHop. URLs not currently extracted."""
    seen = set()
    out = []
    base = f"https://www.renthop.com/{area_path}"
    for page_n in range(1, max_pages + 1):
        url = base if page_n == 1 else f"{base}?page={page_n}"
        status, html = await fetch_page(ctx, url, wait_ms=3500)
        if status != 200:
            log(f"  renthop {area_path} p{page_n} status={status}, stopping")
            break
        addrs = set()
        for m in re.finditer(r'(?:address|street)[^\w]*([0-9][0-9\- A-Za-z]+(?:Street|St|Avenue|Ave|Blvd|Place|Pl|Road|Rd|Drive|Dr|Lane|Ln|Terrace|Ter|Court|Ct|Pkwy|Sq))[^\w]', html):
            addrs.add(m.group(1).strip())
        new = addrs - seen
        log(f"  renthop {area_path} p{page_n}: {len(addrs)} addrs ({len(new)} new)")
        if not new:
            break
        seen.update(addrs)
        for a in new:
            out.append((a, None, None))
        await asyncio.sleep(1.2)
    return out


async def main():
    if not BUILDINGS.exists():
        print(f"missing {BUILDINGS}", file=sys.stderr)
        sys.exit(1)
    records = json.loads(BUILDINGS.read_text())
    addr_idx = build_index(records)
    print(f"DHCR index: {len(addr_idx)} normalized addresses")

    log_lines = []
    def log(msg):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line)
        log_lines.append(line)

    counts = {}  # bbl -> count
    urls = {}    # bbl -> first known Zumper URL for that building
    prices = {}  # bbl -> lowest advertised rent seen for that building
    matched_addrs = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )

        all_listings = []  # list of (addr, url)
        for area in ["manhattan-ny", "brooklyn-ny"]:
            log(f"== Zumper {area} ==")
            try:
                pairs = await scrape_zumper(ctx, area, log=log)
                all_listings.extend(pairs)
            except Exception as e:
                log(f"  zumper {area} fatal: {e}")

        for path in ["apartments-for-rent/manhattan-ny", "apartments-for-rent/brooklyn-ny"]:
            log(f"== RentHop {path} ==")
            try:
                pairs = await scrape_renthop(ctx, path, log=log)
                all_listings.extend(pairs)
            except Exception as e:
                log(f"  renthop {path} fatal: {e}")

        await browser.close()

    log(f"raw listings collected: {len(all_listings)}")

    for raw, url, price in all_listings:
        norm = normalize_addr(raw)
        if not norm:
            continue
        bbl = addr_idx.get(norm)
        if not bbl:
            toks = norm.split(" ")
            for n in range(len(toks), 1, -1):
                cand = " ".join(toks[:n])
                if cand in addr_idx:
                    bbl = addr_idx[cand]
                    break
        if not bbl:
            continue
        counts[bbl] = counts.get(bbl, 0) + 1
        if url and bbl not in urls:
            urls[bbl] = url
        # keep the lowest sane rent seen for the building
        if price and 500 <= price <= 50000:
            if bbl not in prices or price < prices[bbl]:
                prices[bbl] = price
        matched_addrs.add(norm)

    log(f"matched listings: {sum(counts.values())} across {len(counts)} buildings")
    log(f"buildings with direct Zumper URLs: {len(urls)}")
    log(f"buildings with a rent price: {len(prices)}")
    log(f"unique normalized matched addresses: {len(matched_addrs)}")

    payload = {"updated": int(time.time()), "counts": counts, "urls": urls, "prices": prices}
    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    LOG.write_text("\n".join(log_lines) + "\n")
    print(f"wrote {OUT} ({len(counts)} buildings with listings)")


if __name__ == "__main__":
    asyncio.run(main())
