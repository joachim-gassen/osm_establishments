from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
import geopandas as gpd
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By


INPUT_GEOJSON = Path("data/pulled/business_places.geojson")
OUTPUT_JSON = Path("data/pulled/legal_web_pages.json")
CACHE_DIR = Path("data/cache/web_scrape")
RESCRAPE_DATA = False
TEARDOWN_CACHE_FOLDER = False

USER_AGENT = "osm-establishments-legal-scraper/0.1"
REQUEST_TIMEOUT = 20
REQUEST_PAUSE = 1.0
BROWSER_PAGE_LOAD_TIMEOUT = 30
BROWSER_RENDER_WAIT = 2.0

LEGAL_LINK_KEYWORDS = (
    "impressum",
    "imprint",
    "legal",
    "legal-notice",
    "legal_notice",
    "legalnotice",
    "kontakt",
    "contact",
    "about",
    "ueber-uns",
    "uber-uns",
    "datenschutz",
    "privacy",
    "terms",
    "agb",
)
LEGAL_LINK_PRIORITIES = {
    "impressum": 100,
    "imprint": 90,
    "legal notice": 80,
    "legal": 55,
    "kontakt": 45,
    "contact": 45,
    "datenschutz": 25,
    "privacy": 25,
    "terms": 20,
    "agb": 20,
    "about": 15,
    "ueber uns": 15,
    "uber uns": 15,
}
COOKIE_REJECT_PATTERN = re.compile(
    r"\b(reject|decline|necessary only|essential only|nur notwendige|ablehnen|"
    r"nicht akzeptieren|auswahl speichern)\b",
    re.IGNORECASE,
)
COOKIE_ACCEPT_PATTERN = re.compile(
    r"\b(accept all|accept|agree|allow all|ok|okay|got it|continue|zustimmen|"
    r"akzeptieren|alle akzeptieren|einverstanden|verstanden)\b",
    re.IGNORECASE,
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@dataclass
class Page:
    url: str
    status_code: int | None
    page_role: str
    title: str | None
    text: str
    html: str


def create_browser() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1200")
    options.add_argument(f"--user-agent={USER_AGENT}")

    browser = webdriver.Chrome(options=options)
    browser.set_page_load_timeout(BROWSER_PAGE_LOAD_TIMEOUT)
    return browser


def tear_down_cache_dir() -> None:
    if CACHE_DIR.exists():
        logger.info("Removing web scrape cache directory %s", CACHE_DIR)
        shutil.rmtree(CACHE_DIR)

    cache_parent = CACHE_DIR.parent
    if cache_parent.exists() and not any(cache_parent.iterdir()):
        cache_parent.rmdir()


def scrape_cache_path(row: pd.Series) -> Path:
    osm_type = str(row.get("osm_type", "unknown")).strip() or "unknown"
    osm_id = str(row.get("osm_id", "unknown")).strip() or "unknown"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{osm_type}_{osm_id}")
    return CACHE_DIR / f"{safe_name}.json"


def write_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_url(raw_url: str) -> str | None:
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None

    url = raw_url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    parsed = urlparse(url)
    if not parsed.netloc:
        return None

    return parsed.geturl()


def domain_from_url(url: str) -> str:
    host = urlparse(url).hostname or ""
    host = host.lower().removeprefix("www.")

    parts = host.split(".")
    if len(parts) <= 2:
        return host

    if len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"}:
        return ".".join(parts[-3:])

    return ".".join(parts[-2:])


def fetch_url(browser: webdriver.Chrome, url: str, page_role: str) -> Page:
    logger.info("Rendering URL %s", url)

    try:
        browser.get(url)
    except TimeoutException:
        logger.warning("Browser timed out while loading %s; continuing with partial page", url)

    time.sleep(BROWSER_RENDER_WAIT)
    click_cookie_banner(browser)
    time.sleep(0.5)

    html = browser.page_source
    soup = BeautifulSoup(html, "html.parser")

    for element in soup(["script", "style", "noscript", "svg"]):
        element.extract()

    text = " ".join(soup.get_text(" ").split())
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    return Page(browser.current_url, None, page_role, title, text, html)


def click_cookie_banner(browser: webdriver.Chrome) -> None:
    for pattern_name, pattern in (
        ("reject/necessary", COOKIE_REJECT_PATTERN),
        ("accept", COOKIE_ACCEPT_PATTERN),
    ):
        clicked = click_matching_cookie_control(browser, pattern)
        if clicked:
            logger.info("Dismissed cookie banner using %s control", pattern_name)
            return


def click_matching_cookie_control(browser: webdriver.Chrome, pattern: re.Pattern[str]) -> bool:
    selectors = [
        (By.TAG_NAME, "button"),
        (By.TAG_NAME, "a"),
        (By.CSS_SELECTOR, "[role='button']"),
        (By.CSS_SELECTOR, "input[type='button'], input[type='submit']"),
    ]

    for by, selector in selectors:
        try:
            elements = browser.find_elements(by, selector)
        except WebDriverException:
            continue

        for element in elements[:80]:
            try:
                label = " ".join(
                    value
                    for value in (
                        element.text,
                        element.get_attribute("value"),
                        element.get_attribute("aria-label"),
                        element.get_attribute("title"),
                    )
                    if value
                )
                if not label or not pattern.search(label):
                    continue

                browser.execute_script("arguments[0].click();", element)
                return True
            except WebDriverException:
                continue

    return False


def lookup_rdap(session: requests.Session, domain: str) -> dict[str, Any]:
    url = f"https://rdap.org/domain/{domain}"
    logger.info("Looking up RDAP for domain=%s", domain)

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            return {"status_code": 404, "registrant_candidates": []}

        response.raise_for_status()
        data = response.json()
    except requests.RequestException as error:
        logger.warning("RDAP lookup failed for domain=%s error=%s", domain, error)
        return {"status_code": None, "registrant_candidates": [], "error": str(error)}

    return {
        "status_code": response.status_code,
        "registrant_candidates": extract_rdap_entities(data),
    }


def extract_rdap_entities(data: dict[str, Any]) -> list[str]:
    entities = []
    for entity in data.get("entities", []):
        roles = set(entity.get("roles", []))
        if roles and roles.isdisjoint({"registrant", "administrative", "technical"}):
            continue

        vcard = entity.get("vcardArray", [])
        if len(vcard) < 2:
            continue

        for item in vcard[1]:
            if not item or item[0] not in {"fn", "org"}:
                continue
            value = item[-1]
            if isinstance(value, str) and value.strip():
                entities.append(value.strip())

    return sorted(set(entities))


def discover_legal_links(homepage: Page, max_links: int = 5) -> list[str]:
    soup = BeautifulSoup(homepage.html, "html.parser")
    scored_links = []
    homepage_domain = domain_from_url(homepage.url)

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        label = " ".join(anchor.get_text(" ").split())
        link = urljoin(homepage.url, href).split("#", 1)[0]
        score = score_legal_link(homepage_domain, link, label)

        if score == 0:
            continue

        scored_links.append((score, link))

    unique_links = []
    seen = set()
    for _, link in sorted(scored_links, reverse=True):
        if link in seen:
            continue
        seen.add(link)
        unique_links.append(link)

    return unique_links[:max_links]


def score_legal_link(homepage_domain: str, link: str, label: str) -> int:
    parsed = urlparse(link)
    haystack = f"{parsed.path} {parsed.query} {label}"
    normalized = re.sub(r"[^a-z0-9äöüß]+", " ", haystack.lower())
    padded = f" {normalized} "

    score = 0
    for keyword, priority in LEGAL_LINK_PRIORITIES.items():
        if f" {keyword} " in padded:
            score = max(score, priority)

    if score == 0:
        return 0

    if domain_from_url(link) == homepage_domain:
        score += 10
    else:
        score -= 30

    return max(score, 0)


def classify_page_role(url: str, title: str | None = None) -> str:
    parsed = urlparse(url)
    haystack = f"{parsed.path} {parsed.query} {title or ''}"
    normalized = re.sub(r"[^a-z0-9äöüß]+", " ", haystack.lower())
    padded = f" {normalized} "

    if " impressum " in padded:
        return "impressum"
    if " imprint " in padded or " legal notice " in padded:
        return "imprint"
    if " kontakt " in padded or " contact " in padded:
        return "contact"
    if " datenschutz " in padded or " privacy " in padded:
        return "privacy"
    if " agb " in padded or " terms " in padded:
        return "terms"
    if " legal " in padded:
        return "legal"
    if " about " in padded or " ueber uns " in padded or " uber uns " in padded:
        return "about"
    return "other"


def page_to_dict(page: Page) -> dict[str, Any]:
    return {
        "page_role": page.page_role,
        "url": page.url,
        "title": page.title,
        "status_code": page.status_code,
        "text": page.text,
        "text_length": len(page.text),
    }


def scrape_website_for_legal_info(
    session: requests.Session,
    browser: webdriver.Chrome,
    row: pd.Series,
) -> dict[str, Any]:
    url = normalize_url(row.get("website", ""))
    if url is None:
        return {}

    domain = domain_from_url(url)
    result: dict[str, Any] = {
        "osm_type": row.get("osm_type"),
        "osm_id": row.get("osm_id"),
        "name": row.get("name"),
        "website": url,
        "domain": domain,
    }

    result["rdap"] = lookup_rdap(session, domain)
    time.sleep(REQUEST_PAUSE)

    pages = []
    try:
        homepage = fetch_url(browser, url, "homepage")
    except WebDriverException as error:
        logger.warning("Homepage fetch failed for url=%s error=%s", url, error)
        result["fetch_error"] = str(error)
        return result

    pages.append(homepage)
    legal_links = discover_legal_links(homepage)
    result["legal_links"] = legal_links

    for link in legal_links:
        time.sleep(REQUEST_PAUSE)
        try:
            page = fetch_url(browser, link, classify_page_role(link))
            pages.append(page)
        except WebDriverException as error:
            logger.warning("Legal page fetch failed for url=%s error=%s", link, error)

    result["pages"] = [page_to_dict(page) for page in pages]
    return result


def load_establishments(input_geojson: Path) -> pd.DataFrame:
    return pd.DataFrame(gpd.read_file(input_geojson).drop(columns="geometry"))


def run_web_scrape(input_geojson: Path, output_json: Path, limit: int) -> None:
    if output_json.exists() and not RESCRAPE_DATA:
        logger.info("Using existing web scrape output %s", output_json)
        if TEARDOWN_CACHE_FOLDER:
            tear_down_cache_dir()
        return

    df = load_establishments(input_geojson)
    websites = df[df["website"].notna() & (df["website"].str.strip() != "")]
    websites = websites.drop_duplicates(subset=["website"]).head(limit)

    logger.info("Scraping legal web evidence for %s websites", len(websites))

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    results = []
    browser: webdriver.Chrome | None = None
    try:
        for _, row in websites.iterrows():
            cache_path = scrape_cache_path(row)
            if cache_path.exists():
                logger.info(
                    "Using cached web scrape for osm_type=%s osm_id=%s",
                    row.get("osm_type"),
                    row.get("osm_id"),
                )
                results.append(json.loads(cache_path.read_text(encoding="utf-8")))
                continue

            if browser is None:
                browser = create_browser()

            result = scrape_website_for_legal_info(session, browser, row)
            if not result:
                continue
            write_json(result, cache_path)
            results.append(result)
    finally:
        if browser is not None:
            browser.quit()

    write_json(results, output_json)
    if TEARDOWN_CACHE_FOLDER:
        tear_down_cache_dir()

    logger.info("Wrote %s", output_json)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape rendered website pages that may contain legal entity information."
    )
    parser.add_argument("--input", type=Path, default=INPUT_GEOJSON)
    parser.add_argument("--output-json", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--limit", type=int, default=25)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_web_scrape(args.input, args.output_json, args.limit)
