"""
AI Extraction & Catalog Resolution Service (Milestone 3).

Provides dual-engine parsing for customer order requests:
1. Live LLM Parser: Communicates with OpenAI-compatible APIs using structured JSON schema mode.
2. Rule-Based Fallback Parser: Offline deterministic extractor for environments without live API keys.

Guarantees backend database authority: customer input and AI output can never override product
pricing or stock levels because prices are exclusively loaded from catalog records.
"""
import difflib
import json
import re
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    DraftItemMatchStatus,
    DraftOrder,
    DraftOrderItem,
    DraftOrderStatus,
    Product,
)
from app.schemas import ExtractedOrderItem, ParsedOrderRequest

# Initialize OpenAI client if openai library is available
try:
    import openai
    from openai import APITimeoutError, OpenAI, OpenAIError
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "dozen": 12,
}

SINGULAR_EXCEPTIONS = {
    "mouse",
    "house",
    "wireless",
    "glass",
    "lens",
    "status",
    "series",
    "bus",
    "business",
}


def singularize_word(word: str) -> str:
    """
    Normalizes plural English nouns to their singular form for resilient catalog matching.
    e.g. 'keyboards' -> 'keyboard', 'mice' -> 'mouse', 'cables' -> 'cable'.
    """
    w = word.lower().strip()
    if w in SINGULAR_EXCEPTIONS:
        return w
    if w == "mice":
        return "mouse"
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("shes", "ches", "xes", "zes", "sses")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def singularize_text(text: str) -> str:
    """
    Applies singularization across each individual word of a phrase.
    """
    return " ".join(singularize_word(w) for w in text.split())


def get_openai_client() -> Optional["OpenAI"]:
    """
    Returns an instantiated OpenAI client if API key is configured.
    """
    if not OPENAI_AVAILABLE:
        return None
    if not settings.llm_api_key or settings.llm_api_key.strip().lower() in ("mock", "test", "none", ""):
        return None
    return OpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout_seconds,
    )


def mock_parse_customer_message(message: str) -> ParsedOrderRequest:
    """
    Deterministic rule-based fallback parser used when no live LLM API key is configured.
    Robustly extracts product names and quantities from natural language messages.
    Supports single and multi-item requests, number words, and handles missing quantities.
    """
    items: List[ExtractedOrderItem] = []
    text = message.strip()
    if not text:
        return ParsedOrderRequest(items=[], intent="Empty customer message")

    # 1. Clean greeting prefix
    text = re.sub(
        r"^(?:hello|hi|hey|good\s+(?:morning|afternoon|evening))[,\s]*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # 2. Clean conversational request lead-ins
    lead_in_pattern = re.compile(
        r"^(?:"
        r"(?:i\s+)?(?:would\s+like|'d\s+like|want|need|wish)(?:\s+to\s+(?:order|buy|get|have|purchase))?"
        r"|(?:can|could)\s+(?:i|you)(?:\s+please)?(?:\s+(?:have|get|order|buy|send|give))(?:\s+me)?"
        r"|please\s+(?:send|give|order|get|buy)(?:\s+me)?"
        r"|please"
        r"|(?:order|send|give|get|buy)(?:\s+me)?"
        r")\s+",
        re.IGNORECASE,
    )

    # 3. Split into item clauses by comma, 'and', '&', semicolon, or period
    raw_clauses = re.split(
        r",\s*(?:and\s+)?|;\s*|\s+and\s+|\s*&\s*|\n+|\.\s*",
        text,
        flags=re.IGNORECASE,
    )

    for raw in raw_clauses:
        clause = raw.strip()
        if not clause:
            continue

        # Strip conversational lead-ins from the clause (run twice in case of chained phrases)
        clause = lead_in_pattern.sub("", clause).strip()
        clause = lead_in_pattern.sub("", clause).strip()

        # Strip trailing courtesies (e.g. "please", "thanks", "asap")
        clause = re.sub(
            r"\s+(?:please|thanks|thank\s+you|thx|asap|now)[\.\!]?$",
            "",
            clause,
            flags=re.IGNORECASE,
        ).strip(",.!? ")

        if not clause:
            continue

        # Check for leading quantity (digits or number words like 'two', '3x', '5 pcs of')
        qty_match = re.match(
            r"^(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen)\s*(?:x\b|pcs?\b|pieces?\b|units?\b|of\b)?\s*(.+)$",
            clause,
            re.IGNORECASE,
        )

        if qty_match:
            num_str = qty_match.group(1).lower()
            quantity: Optional[int] = int(num_str) if num_str.isdigit() else NUMBER_WORDS.get(num_str)
            prod_name = qty_match.group(2).strip()
        else:
            # Check for trailing quantity notation (e.g. "keyboard x 2", "mice qty: 3")
            trailing_match = re.match(
                r"^(.+?)\s*(?:x|\*|qty[:\s]*|\(|quantity[:\s]*)\s*(\d+)\s*\)?$",
                clause,
                re.IGNORECASE,
            )
            if trailing_match:
                prod_name = trailing_match.group(1).strip()
                quantity = int(trailing_match.group(2))
            else:
                quantity = None
                prod_name = clause.strip()

        # Clean product text
        prod_name = prod_name.strip(" .,-")
        if prod_name and len(prod_name) >= 2:
            items.append(ExtractedOrderItem(product_name=prod_name, quantity=quantity))

    # Fallback if no items extracted
    if not items and text:
        items.append(ExtractedOrderItem(product_name=text, quantity=None))

    return ParsedOrderRequest(
        items=items,
        intent="Parsed via deterministic rule-based extractor",
    )


def parse_customer_message_with_llm(message: str) -> ParsedOrderRequest:
    """
    Converts unstructured natural language customer messages into structured product/quantity requests.
    Handles:
    - Live OpenAI LLM calls with JSON schema mode.
    - AI timeouts (HTTP 504).
    - Malformed responses (HTTP 502).
    - Offline fallback / mock mode when no LLM key is configured.
    """
    client = get_openai_client()

    # If no live API key is set, use the robust deterministic fallback parser
    if client is None:
        return mock_parse_customer_message(message)

    system_prompt = (
        "You are an AI order processing assistant for an inventory system. "
        "Your task is to extract product names and requested quantities from customer messages.\n"
        "Rules:\n"
        "1. Extract every distinct product mentioned by the customer.\n"
        "2. If a quantity is explicitly stated, include it as an integer.\n"
        "3. If a quantity is missing, vague, or not specified, set 'quantity' to null.\n"
        "4. Return ONLY a valid JSON object matching this schema:\n"
        "{\n"
        '  "items": [\n'
        '    {"product_name": "string", "quantity": integer_or_null}\n'
        "  ],\n"
        '  "intent": "short summary of message or clarification needed"\n'
        "}"
    )

    try:
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            response_format={"type": "json_object"},
            timeout=settings.llm_timeout_seconds,
        )

        raw_content = response.choices[0].message.content
        if not raw_content:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="AI returned an empty response.",
            )

        parsed_json = json.loads(raw_content)
        return ParsedOrderRequest.model_validate(parsed_json)

    except APITimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"AI service timed out after {settings.llm_timeout_seconds}s while processing message.",
        ) from exc

    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI returned a malformed, non-JSON response.",
        ) from exc

    except OpenAIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM API provider error: {exc}",
        ) from exc


def match_product_in_catalog(
    query_text: str,
    db: Session,
) -> Tuple[Optional[Product], DraftItemMatchStatus, List[Product]]:
    """
    Matches raw product text from customer against products in the database.
    Applies multi-stage matching:
    1. Exact SKU match (case-insensitive)
    2. Exact Name match (original or singularized)
    3. Substring match on name or category (original or singularized)
    4. Multi-word token conjunction (all significant words match name/category)
    5. Token disjunction (any significant word matches)
    6. Fuzzy typo matching via difflib (cutoff=0.7 for active products)
    Returns:
        (matched_product, match_status, candidate_list)
    """
    clean_query = query_text.strip().lower()
    singular_query = singularize_text(clean_query)

    # 1. Exact SKU match
    sku_match = db.query(Product).filter(func.upper(Product.sku) == clean_query.upper()).first()
    if sku_match:
        if not sku_match.is_active:
            return sku_match, DraftItemMatchStatus.INACTIVE, [sku_match]
        return sku_match, DraftItemMatchStatus.MATCHED, [sku_match]

    # 2. Exact Name match (case-insensitive on original or singularized form)
    for q in (clean_query, singular_query):
        exact_matches = db.query(Product).filter(func.lower(Product.name) == q).all()
        if len(exact_matches) == 1:
            prod = exact_matches[0]
            if not prod.is_active:
                return prod, DraftItemMatchStatus.INACTIVE, exact_matches
            return prod, DraftItemMatchStatus.MATCHED, exact_matches
        elif len(exact_matches) > 1:
            active_exact = [p for p in exact_matches if p.is_active]
            if len(active_exact) == 1:
                return active_exact[0], DraftItemMatchStatus.MATCHED, active_exact
            return None, DraftItemMatchStatus.AMBIGUOUS, active_exact

    # 3. Substring matching on name or category
    for q in (clean_query, singular_query):
        substring_matches = (
            db.query(Product)
            .filter(
                or_(
                    Product.name.ilike(f"%{q}%"),
                    Product.category.ilike(f"%{q}%"),
                )
            )
            .all()
        )

        if len(substring_matches) == 1:
            prod = substring_matches[0]
            if not prod.is_active:
                return prod, DraftItemMatchStatus.INACTIVE, substring_matches
            return prod, DraftItemMatchStatus.MATCHED, substring_matches

        if len(substring_matches) > 1:
            active_candidates = [p for p in substring_matches if p.is_active]
            if len(active_candidates) == 1:
                return active_candidates[0], DraftItemMatchStatus.MATCHED, active_candidates
            elif len(active_candidates) > 1:
                return None, DraftItemMatchStatus.AMBIGUOUS, active_candidates
            else:
                return substring_matches[0], DraftItemMatchStatus.INACTIVE, substring_matches

    # 4. Multi-word all-token conjunction (e.g. "wireless mice" -> words: "wireless", "mouse")
    words = [singularize_word(w) for w in re.split(r"[\s\-_]+", clean_query) if len(w) >= 3]
    if len(words) > 1:
        from sqlalchemy import and_
        all_token_filters = [
            or_(
                Product.name.ilike(f"%{w}%"),
                Product.category.ilike(f"%{w}%"),
            )
            for w in words
        ]
        all_token_matches = db.query(Product).filter(and_(*all_token_filters)).all()
        if len(all_token_matches) == 1:
            prod = all_token_matches[0]
            if not prod.is_active:
                return prod, DraftItemMatchStatus.INACTIVE, all_token_matches
            return prod, DraftItemMatchStatus.MATCHED, all_token_matches
        elif len(all_token_matches) > 1:
            active_tokens = [p for p in all_token_matches if p.is_active]
            if len(active_tokens) == 1:
                return active_tokens[0], DraftItemMatchStatus.MATCHED, active_tokens
            elif len(active_tokens) > 1:
                return None, DraftItemMatchStatus.AMBIGUOUS, active_tokens

    # 5. Token disjunction (any significant word matches)
    if words:
        any_token_filters = [
            or_(
                Product.name.ilike(f"%{w}%"),
                Product.category.ilike(f"%{w}%"),
            )
            for w in words
        ]
        token_matches = db.query(Product).filter(or_(*any_token_filters)).all()
        if len(token_matches) == 1:
            prod = token_matches[0]
            if not prod.is_active:
                return prod, DraftItemMatchStatus.INACTIVE, token_matches
            return prod, DraftItemMatchStatus.MATCHED, token_matches
        elif len(token_matches) > 1:
            active_tokens = [p for p in token_matches if p.is_active]
            if len(active_tokens) == 1:
                return active_tokens[0], DraftItemMatchStatus.MATCHED, active_tokens
            elif len(active_tokens) > 1:
                return None, DraftItemMatchStatus.AMBIGUOUS, active_tokens

    # 6. Fuzzy matching fallback for typos using difflib (active products only)
    active_products = db.query(Product).filter(Product.is_active == True).all()
    if active_products:
        name_map = {p.name.lower(): p for p in active_products}
        all_active_names = list(name_map.keys())

        # Check full name similarity with cutoff 0.70
        close_names = difflib.get_close_matches(clean_query, all_active_names, n=3, cutoff=0.7)
        if not close_names and singular_query != clean_query:
            close_names = difflib.get_close_matches(singular_query, all_active_names, n=3, cutoff=0.7)

        # If full-name match didn't trigger, check word-level similarity (e.g. "keybaord" -> "keyboard")
        if not close_names:
            matched_by_word = []
            for prod in active_products:
                prod_words = [w for w in re.split(r"[\s\-_]+", prod.name.lower()) if len(w) >= 3]
                for pw in prod_words:
                    for qw in (clean_query, singular_query):
                        if difflib.SequenceMatcher(None, qw, pw).ratio() >= 0.75:
                            if prod not in matched_by_word:
                                matched_by_word.append(prod)
                            break
            if len(matched_by_word) == 1:
                return matched_by_word[0], DraftItemMatchStatus.MATCHED, matched_by_word
            elif len(matched_by_word) > 1:
                return None, DraftItemMatchStatus.AMBIGUOUS, matched_by_word

        if len(close_names) == 1:
            prod = name_map[close_names[0]]
            return prod, DraftItemMatchStatus.MATCHED, [prod]
        elif len(close_names) > 1:
            candidates = [name_map[n] for n in close_names]
            return None, DraftItemMatchStatus.AMBIGUOUS, candidates

    # 7. No matches found
    return None, DraftItemMatchStatus.UNKNOWN_PRODUCT, []


def build_draft_order_from_parsed(
    customer_id: int,
    raw_message: str,
    parsed: ParsedOrderRequest,
    db: Session,
) -> DraftOrder:
    """
    Transforms extracted items into a DraftOrder model with DraftOrderItem line items.
    Applies matching rules, detects ambiguities/missing quantities, and sets draft status.
    NOTE: NEVER changes product inventory or creates an official order.
    """
    draft_items: List[DraftOrderItem] = []
    clarification_notes: List[str] = []
    estimated_total = Decimal("0.00")
    needs_clarification = False

    # Cache product lookups to avoid repeated database queries across multi-item requests
    lookup_cache: Dict[str, Tuple[Optional[Product], DraftItemMatchStatus, List[Product]]] = {}

    if not parsed.items:
        needs_clarification = True
        clarification_notes.append("No products could be identified in the message.")

    for item in parsed.items:
        cache_key = item.product_name.strip().lower()
        if cache_key in lookup_cache:
            matched_prod, match_status, candidates = lookup_cache[cache_key]
        else:
            matched_prod, match_status, candidates = match_product_in_catalog(item.product_name, db)
            lookup_cache[cache_key] = (matched_prod, match_status, candidates)

        # Check for missing quantity
        if match_status == DraftItemMatchStatus.MATCHED:
            if item.quantity is None or item.quantity <= 0:
                match_status = DraftItemMatchStatus.MISSING_QUANTITY
                needs_clarification = True
                clarification_notes.append(
                    f"Quantity missing for '{matched_prod.name}'. Please specify how many are needed."
                )

        possible_matches_str: Optional[str] = None
        unit_price: Optional[Decimal] = None
        total_price: Optional[Decimal] = None
        product_id: Optional[int] = None
        matched_name: Optional[str] = None

        if match_status == DraftItemMatchStatus.MATCHED and matched_prod:
            product_id = matched_prod.id
            matched_name = matched_prod.name
            unit_price = Decimal(str(matched_prod.price))
            total_price = unit_price * Decimal(item.quantity)
            estimated_total += total_price

        elif match_status == DraftItemMatchStatus.UNKNOWN_PRODUCT:
            needs_clarification = True
            clarification_notes.append(f"Product '{item.product_name}' was not found in our catalog.")

        elif match_status == DraftItemMatchStatus.AMBIGUOUS:
            needs_clarification = True
            candidate_labels = [f"{p.name} (SKU: {p.sku})" for p in candidates]
            possible_matches_str = "; ".join(candidate_labels)
            clarification_notes.append(
                f"'{item.product_name}' is ambiguous. Matching candidates: {possible_matches_str}"
            )

        elif match_status == DraftItemMatchStatus.INACTIVE and matched_prod:
            needs_clarification = True
            product_id = matched_prod.id
            matched_name = matched_prod.name
            clarification_notes.append(f"Product '{matched_prod.name}' is currently inactive/discontinued.")

        elif match_status == DraftItemMatchStatus.MISSING_QUANTITY and matched_prod:
            product_id = matched_prod.id
            matched_name = matched_prod.name
            unit_price = Decimal(str(matched_prod.price))

        draft_item = DraftOrderItem(
            raw_product_text=item.product_name,
            product_id=product_id,
            matched_product_name=matched_name,
            quantity=item.quantity,
            unit_price=unit_price,
            total_price=total_price,
            match_status=match_status,
            possible_matches=possible_matches_str,
        )
        draft_items.append(draft_item)

    # Determine overall draft status
    overall_status = (
        DraftOrderStatus.REQUIRES_CLARIFICATION
        if needs_clarification
        else DraftOrderStatus.PENDING_CONFIRMATION
    )

    combined_notes = (
        "; ".join(clarification_notes)
        if clarification_notes
        else (parsed.intent or "All requested items matched. Ready for confirmation.")
    )

    draft_order = DraftOrder(
        customer_id=customer_id,
        raw_message=raw_message,
        status=overall_status,
        notes=combined_notes,
        estimated_total=estimated_total,
        items=draft_items,
    )
    return draft_order
