from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv


SECRETS_ENV = Path("secrets.env")
INPUT_JSON = Path("data/pulled/legal_web_pages.json")
OUTPUT_CSV = Path("data/generated/osm_establishments_legal_info.csv")
CACHE_DIR = Path("data/cache/llm_parse")
INSTRUCTIONS_PATH = Path("code/llm_instructions.md")
DEFAULT_MODEL = "openai/gpt-5.4-mini"
FULL_TEXT_PAGE_ROLES = {"impressum", "imprint", "legal"}
SNIPPET_PAGE_ROLES = {"homepage", "contact", "about"}
LOW_PRIORITY_PAGE_ROLES = {"privacy", "terms"}
FULL_TEXT_LIMIT = 14000
SNIPPET_TEXT_LIMIT = 7000
LOW_PRIORITY_TEXT_LIMIT = 3500
CONTEXT_WINDOW = 900
EVIDENCE_KEYWORDS = (
    "impressum",
    "anbieter",
    "betreiber",
    "firma",
    "unternehmen",
    "vertreten durch",
    "geschäftsführer",
    "geschaeftsfuehrer",
    "registergericht",
    "handelsregister",
    "amtsgericht",
    "hrb",
    "hra",
    "ust-id",
    "ustid",
    "ust-idnr",
    "umsatzsteuer",
    "steuernummer",
    "tax number",
    "vat",
    "registered office",
    "company number",
    "responsible for content",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def configure_litellm_logging() -> None:
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    for logger_name in ("LiteLLM", "LiteLLM Proxy", "LiteLLM Router", "litellm"):
        litellm_logger = logging.getLogger(logger_name)
        litellm_logger.handlers.clear()
        litellm_logger.propagate = False
        litellm_logger.disabled = True


def litellm_completion(**kwargs: Any) -> Any:
    configure_litellm_logging()
    import litellm

    litellm.suppress_debug_info = True
    litellm.set_verbose = False
    litellm.log_level = "ERROR"
    return litellm.completion(**kwargs)


def load_instructions(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_project_env() -> None:
    load_dotenv(SECRETS_ENV)


def parse_cache_path(result: dict[str, Any]) -> Path:
    osm_type = str(result.get("osm_type", "unknown")).strip() or "unknown"
    osm_id = str(result.get("osm_id", "unknown")).strip() or "unknown"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{osm_type}_{osm_id}")
    return CACHE_DIR / f"{safe_name}.json"


def osm_log_context(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "osm_type": result.get("osm_type"),
        "osm_id": result.get("osm_id"),
        "website": result.get("website"),
    }


def write_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_evidence_packet(result: dict[str, Any]) -> dict[str, Any]:
    pages = result.get("pages", [])
    return {
        "establishment": {
            "osm_type": result.get("osm_type"),
            "osm_id": result.get("osm_id"),
            "name": result.get("name"),
            "website": result.get("website"),
            "domain": result.get("domain"),
        },
        "dns_registrant_candidates": result.get("rdap", {}).get(
            "registrant_candidates", []
        ),
        "legal_links": result.get("legal_links", []),
        "pages": curate_pages_for_llm(pages),
    }


def curate_pages_for_llm(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked_pages = sorted(pages, key=page_priority, reverse=True)
    curated_pages = []

    for page in ranked_pages:
        text = page.get("text") or ""
        page_role = page.get("page_role") or "other"

        if page_role in FULL_TEXT_PAGE_ROLES:
            selected_text = truncate_text(text, FULL_TEXT_LIMIT)
            selection_strategy = "full_text_priority_page"
        elif page_role in SNIPPET_PAGE_ROLES:
            selected_text = extract_keyword_contexts(text, SNIPPET_TEXT_LIMIT)
            selection_strategy = "keyword_contexts"
        elif page_role in LOW_PRIORITY_PAGE_ROLES:
            selected_text = extract_keyword_contexts(text, LOW_PRIORITY_TEXT_LIMIT)
            selection_strategy = "low_priority_keyword_contexts"
        else:
            selected_text = extract_keyword_contexts(text, LOW_PRIORITY_TEXT_LIMIT)
            selection_strategy = "other_keyword_contexts"

        if not selected_text.strip():
            continue

        curated_pages.append(
            {
                "page_role": page_role,
                "url": page.get("url"),
                "title": page.get("title"),
                "original_text_length": page.get("text_length", len(text)),
                "selection_strategy": selection_strategy,
                "text": selected_text,
            }
        )

    return curated_pages


def page_priority(page: dict[str, Any]) -> int:
    role = page.get("page_role")
    priorities = {
        "impressum": 100,
        "imprint": 95,
        "legal": 80,
        "contact": 60,
        "homepage": 50,
        "about": 40,
        "privacy": 20,
        "terms": 15,
    }
    return priorities.get(role, 0)


def extract_keyword_contexts(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    lower_text = text.lower()
    windows = []
    for keyword in EVIDENCE_KEYWORDS:
        start = 0
        while True:
            index = lower_text.find(keyword, start)
            if index == -1:
                break
            windows.append(
                (
                    max(0, index - CONTEXT_WINDOW),
                    min(len(text), index + len(keyword) + CONTEXT_WINDOW),
                )
            )
            start = index + len(keyword)

    if not windows:
        return truncate_text(text, max_chars)

    merged_windows = merge_windows(windows)
    snippets = []
    for start, end in merged_windows:
        snippet = text[start:end].strip()
        if snippet:
            snippets.append(snippet)

        joined = "\n\n[...]\n\n".join(snippets)
        if len(joined) >= max_chars:
            return truncate_text(joined, max_chars)

    return truncate_text("\n\n[...]\n\n".join(snippets), max_chars)


def merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not windows:
        return []

    windows = sorted(windows)
    merged = [windows[0]]
    for start, end in windows[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}\n\n[TRUNCATED]"


def parse_with_llm(
    evidence_packet: dict[str, Any],
    instructions: str,
    model: str,
) -> dict[str, Any]:
    response = litellm_completion(
        model=model,
        messages=[
            {
                "role": "system",
                "content": instructions,
            },
            {
                "role": "user",
                "content": json.dumps(
                    evidence_packet,
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
        response_format={"type": "json_object"},
    )

    content = response["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    return {
        "model": model,
        "raw_content": content,
        "parsed_json": parsed,
        "normalized_parsed": normalize_parsed_result(parsed),
    }


def normalize_parsed_result(parsed: dict[str, Any]) -> dict[str, Any]:
    address = parsed.get("address") or {}
    handelsregister = parsed.get("handelsregister") or {}

    confidence = parsed.get("confidence")
    if confidence not in {"high", "medium", "low", "none"}:
        confidence = "none"

    warnings = parsed.get("warnings")
    if not isinstance(warnings, list):
        warnings = []

    return {
        "legal_name": parsed.get("legal_name"),
        "address": {
            "full_address": address.get("full_address"),
            "street": address.get("street"),
            "house_number": address.get("house_number"),
            "postal_code": address.get("postal_code"),
            "city": address.get("city"),
            "country": address.get("country"),
        },
        "handelsregister": {
            "court": normalize_register_court(handelsregister.get("court")),
            "register_type": handelsregister.get("register_type"),
            "register_number": handelsregister.get("register_number"),
            "raw_text": handelsregister.get("raw_text"),
        },
        "vat_number": parsed.get("vat_number"),
        "steuer_number": parsed.get("steuer_number"),
        "confidence": confidence,
        "evidence_summary": parsed.get("evidence_summary"),
        "warnings": [str(warning) for warning in warnings],
    }


def normalize_register_court(court: Any) -> str | None:
    if not isinstance(court, str) or not court.strip():
        return None

    normalized = court.strip()
    normalized = re.sub(
        r"^(?:registergericht|register court|court of registration)\s*[:.-]?\s*",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"^(?:amtsgericht|ag)\s+",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = normalized.strip(" ,.;:-")

    return normalized or None


def flatten_result(result: dict[str, Any]) -> dict[str, Any]:
    parsed = result["llm_parsed"]
    address = parsed["address"]
    handelsregister = parsed["handelsregister"]

    return {
        "osm_type": result.get("osm_type"),
        "osm_id": result.get("osm_id"),
        "name": result.get("name"),
        "website": result.get("website"),
        "domain": result.get("domain"),
        "legal_name": parsed.get("legal_name"),
        "full_address": address.get("full_address"),
        "street": address.get("street"),
        "house_number": address.get("house_number"),
        "postal_code": address.get("postal_code"),
        "city": address.get("city"),
        "country": address.get("country"),
        "register_court": handelsregister.get("court"),
        "register_type": handelsregister.get("register_type"),
        "register_number": handelsregister.get("register_number"),
        "register_raw_text": handelsregister.get("raw_text"),
        "vat_number": parsed.get("vat_number"),
        "steuer_number": parsed.get("steuer_number"),
        "confidence": parsed.get("confidence"),
        "evidence_summary": parsed.get("evidence_summary"),
        "warnings": " | ".join(parsed.get("warnings", [])),
    }


def run_parser(
    input_json: Path,
    output_csv: Path,
    instructions_path: Path,
    model: str,
    limit: int | None,
) -> None:
    instructions = load_instructions(instructions_path)
    results = json.loads(input_json.read_text(encoding="utf-8"))
    if limit is not None:
        results = results[:limit]

    logger.info("Extracting legal info from %s scraped website results with model=%s", len(results), model)

    flattened_rows = []
    for index, result in enumerate(results, start=1):
        context = osm_log_context(result)
        logger.info(
            "Extracting %s/%s osm_type=%s osm_id=%s website=%s",
            index,
            len(results),
            context["osm_type"],
            context["osm_id"],
            context["website"],
        )

        cache_path = parse_cache_path(result)
        if cache_path.exists():
            logger.info(
                "Using cached LLM extraction for osm_type=%s osm_id=%s website=%s",
                context["osm_type"],
                context["osm_id"],
                context["website"],
            )
            verbose_result = json.loads(cache_path.read_text(encoding="utf-8"))
            flattened_rows.append(flatten_result(verbose_result))
            continue

        evidence_packet = build_evidence_packet(result)
        logger.info(
            "Querying LLM for osm_type=%s osm_id=%s website=%s",
            context["osm_type"],
            context["osm_id"],
            context["website"],
        )
        llm_response = parse_with_llm(evidence_packet, instructions, model)
        logger.info(
            "LLM responded for osm_type=%s osm_id=%s website=%s",
            context["osm_type"],
            context["osm_id"],
            context["website"],
        )

        verbose_result = {
            **result,
            "provided_data": {
                "scraped_result": result,
                "llm_evidence_packet": evidence_packet,
            },
            "llm_evidence_packet": evidence_packet,
            "llm_response": llm_response,
            "llm_parsed": llm_response["normalized_parsed"],
        }
        write_json(verbose_result, cache_path)
        flattened_rows.append(flatten_result(verbose_result))

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(flattened_rows).to_csv(output_csv, index=False)

    logger.info("Wrote %s", output_csv)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract legal entity fields from rendered legal evidence pages with an LLM."
    )
    parser.add_argument("--input", type=Path, default=INPUT_JSON)
    parser.add_argument("--output-csv", type=Path, default=OUTPUT_CSV)
    parser.add_argument("--instructions", type=Path, default=INSTRUCTIONS_PATH)
    parser.add_argument("--model", default=os.getenv("LLM_MODEL", DEFAULT_MODEL))
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    load_project_env()
    args = parse_args()
    run_parser(
        input_json=args.input,
        output_csv=args.output_csv,
        instructions_path=args.instructions,
        model=args.model,
        limit=args.limit,
    )
