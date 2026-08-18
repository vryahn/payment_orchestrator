#!/usr/bin/env python
"""The LLM edge on the way IN: heterogeneous PSP decline dialects -> one enum.

Every PSP reports declines in its own private vocabulary. psp-a speaks ISO 8583
numerics ('05', '51'), psp-b a Stripe-like `decline_code`, psp-c an Adyen-like
`refusalReason`, psp-d whatever prose its acquirer's bank happened to send. The
retry state machine in orchestrator.py keys on exactly one vocabulary --
orchestrator.ERROR_CLASSES -- because the routing decision has to be
deterministic and auditable. So the translation problem has to be solved
*before* the engine, not inside it.

    normalize(psp, raw_code=None, raw_message=None) -> Normalized

Three routes, in order:
  1. TABLE    a deterministic (dialect, code) map. Covers the codes that
              actually carry volume. confidence 1.0, zero cost, zero latency.
  2. LLM      only on a table miss. Gemini REST first (three model
              generations), Mistral as the second provider. Structured output
              with the enum constrained by responseSchema, so the model picks a
              class rather than inventing one. Any class outside the enum, or
              confidence below 0.6, is discarded.
  3. FALLBACK no keys, provider error, or low confidence -> 'generic_decline',
              which is the retry policy's own safe default (one failover, then
              stop). The repo runs green with no API keys set.

The LLM never sees a routing decision and never returns one. It answers one
bounded classification question whose entire output space is six strings.
"""
import argparse
import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

from orchestrator import ERROR_CLASSES

TIMEOUT = 8
MIN_CONFIDENCE = 0.6
MAX_MESSAGE_CHARS = 120
GEMINI_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash"]
MISTRAL_MODEL = "mistral-small-latest"

PSP_DIALECT = {
    "psp-a": "iso8583",       # numeric response codes
    "psp-b": "stripe_like",   # snake_case decline_code
    "psp-c": "adyen_like",    # human-ish refusalReason
    "psp-d": "free_text",     # raw bank message, no code at all
}
DEFAULT_DIALECT = "free_text"

# (dialect, normalized code) -> error_class. Deliberately not exhaustive: the
# long tail is what route 2 exists for.
CODE_TABLE = {
    ("iso8583", "01"): "generic_decline",       # refer to card issuer
    ("iso8583", "04"): "fraud_risk",            # pick up card
    ("iso8583", "05"): "generic_decline",       # do not honor
    ("iso8583", "12"): "other",                 # invalid transaction
    ("iso8583", "13"): "other",                 # invalid amount
    ("iso8583", "14"): "invalid_card_info",     # invalid card number
    ("iso8583", "41"): "fraud_risk",            # lost card
    ("iso8583", "43"): "fraud_risk",            # stolen card
    ("iso8583", "51"): "insufficient_funds",
    ("iso8583", "54"): "invalid_card_info",     # expired card
    ("iso8583", "55"): "bank_auth_required",    # incorrect PIN -> re-prompt
    ("iso8583", "57"): "other",                 # txn not permitted to cardholder
    ("iso8583", "59"): "fraud_risk",            # suspected fraud
    ("iso8583", "61"): "insufficient_funds",    # exceeds withdrawal limit
    ("iso8583", "62"): "other",                 # restricted card
    ("iso8583", "65"): "bank_auth_required",    # soft decline, SCA required
    ("iso8583", "82"): "invalid_card_info",     # CVV failure
    ("iso8583", "91"): "other",                 # issuer unavailable
    ("iso8583", "96"): "other",                 # system malfunction

    ("stripe_like", "insufficient_funds"): "insufficient_funds",
    ("stripe_like", "do_not_honor"): "generic_decline",
    ("stripe_like", "generic_decline"): "generic_decline",
    ("stripe_like", "lost_card"): "fraud_risk",
    ("stripe_like", "stolen_card"): "fraud_risk",
    ("stripe_like", "pickup_card"): "fraud_risk",
    ("stripe_like", "fraudulent"): "fraud_risk",
    ("stripe_like", "merchant_blacklist"): "fraud_risk",
    ("stripe_like", "expired_card"): "invalid_card_info",
    ("stripe_like", "incorrect_cvc"): "invalid_card_info",
    ("stripe_like", "incorrect_number"): "invalid_card_info",
    ("stripe_like", "invalid_expiry_year"): "invalid_card_info",
    ("stripe_like", "authentication_required"): "bank_auth_required",
    ("stripe_like", "processing_error"): "other",
    ("stripe_like", "issuer_not_available"): "other",
    ("stripe_like", "try_again_later"): "other",

    ("adyen_like", "refused"): "generic_decline",
    ("adyen_like", "referral"): "generic_decline",
    ("adyen_like", "not enough balance"): "insufficient_funds",
    ("adyen_like", "withdrawal amount exceeded"): "insufficient_funds",
    ("adyen_like", "fraud"): "fraud_risk",
    ("adyen_like", "fraud-cancelled"): "fraud_risk",
    ("adyen_like", "authentication required"): "bank_auth_required",
    ("adyen_like", "3d not authenticated"): "bank_auth_required",
    ("adyen_like", "expired card"): "invalid_card_info",
    ("adyen_like", "invalid card number"): "invalid_card_info",
    ("adyen_like", "cvc declined"): "invalid_card_info",
    ("adyen_like", "blocked card"): "invalid_card_info",
    ("adyen_like", "restricted card"): "invalid_card_info",
    ("adyen_like", "acquirer error"): "other",
    ("adyen_like", "issuer unavailable"): "other",
    ("adyen_like", "transaction not permitted"): "other",
}

# psp-d has no codes at all, only prose. A handful of exact strings are common
# enough to be worth pinning; everything else goes to the model.
MESSAGE_TABLE = {
    ("free_text", "insufficient funds"): "insufficient_funds",
    ("free_text", "do not honor"): "generic_decline",
    ("free_text", "do not honour"): "generic_decline",
    ("free_text", "card expired"): "invalid_card_info",
    ("free_text", "suspected fraud"): "fraud_risk",
    ("free_text", "authentication required"): "bank_auth_required",
    ("free_text", "issuer unavailable"): "other",
}

# The prompt describes each class by what the retry policy DOES with it. A model
# asked to match strings will call 'card blocked by issuer' a fraud signal; a
# model asked which downstream action is correct will notice that retrying is
# futile either way and that the customer needs a different card.
SYSTEM_PROMPT = (
    "You classify payment decline reasons for a routing engine. Pick exactly one "
    "class from the allowed enum. Choose by what the retry policy should DO next, "
    "not by which words look similar.\n"
    "- insufficient_funds: the account simply lacks money or hit a spend limit right now. "
    "The card is fine. Policy: retry the SAME provider later (next billing window); this "
    "has the highest recovery rate of any decline.\n"
    "- bank_auth_required: the issuer wants a step-up authentication (3DS, SCA soft decline, "
    "PIN re-entry). Policy: prompt the customer to authenticate, or reschedule to a channel "
    "where a customer is present. A blind retry cannot succeed.\n"
    "- invalid_card_info: the credentials themselves are wrong, dead, or unusable (expired, "
    "wrong number, bad CVC, card blocked/restricted by the issuer). Policy: STOP retrying and "
    "ask the customer for a different card. Retrying the same credentials never works.\n"
    "- fraud_risk: the issuer or network signalled fraud, theft, loss, or a pick-up-card "
    "instruction. Policy: HARD STOP the whole retry chain permanently. Choose this only on a "
    "real fraud/theft signal, because it is the most destructive outcome.\n"
    "- generic_decline: the issuer refused without saying why ('do not honor', 'refused', "
    "'declined'). Policy: exactly one failover to a different provider, then stop. This is "
    "also the correct answer whenever the message is ambiguous -- it is the safe default.\n"
    "- other: a technical or processing failure on the acquirer/issuer side rather than a "
    "decision about the card (system unavailable, timeout, malformed request, transaction "
    "type not permitted). Policy: same one-failover treatment as generic_decline.\n"
    "Set confidence below 0.6 when the text does not clearly identify one policy. "
    "Never invent a class outside the enum."
)

_CACHE = {}


@dataclass
class Normalized:
    error_class: str
    confidence: float
    source: str          # "table" | "llm" | "fallback"
    provider: str | None
    reasoning: str

    def to_dict(self) -> dict:
        return asdict(self)


def _fallback(reason: str) -> Normalized:
    return Normalized("generic_decline", 0.0, "fallback", None, reason)


def _key(dialect: str, raw: str) -> str:
    return " ".join(str(raw).strip().lower().split())


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def _env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _user_prompt(psp, dialect, raw_code, raw_message):
    msg = (raw_message or "")[:MAX_MESSAGE_CHARS]
    return (
        f"provider: {psp} (decline dialect: {dialect})\n"
        f"raw_code: {raw_code or '(none)'}\n"
        f"raw_message: {msg or '(none)'}"
    )


def _gemini(key, prompt):
    schema = {
        "type": "OBJECT",
        "properties": {
            "error_class": {"type": "STRING", "enum": list(ERROR_CLASSES)},
            "confidence": {"type": "NUMBER"},
            "reasoning": {"type": "STRING"},
        },
        "required": ["error_class", "confidence", "reasoning"],
    }
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    for model in GEMINI_MODELS:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={key}")
        try:
            data = _post_json(url, payload, {})
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text), f"gemini:{model}"
        except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError):
            continue
    return None, None


def _mistral(key, prompt):
    payload = {
        "model": MISTRAL_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system",
             "content": SYSTEM_PROMPT + "\nAllowed error_class values: "
             + ", ".join(ERROR_CLASSES)
             + '. Reply as JSON: {"error_class": ..., "confidence": ..., "reasoning": ...}'},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        data = _post_json("https://api.mistral.ai/v1/chat/completions", payload,
                          {"Authorization": f"Bearer {key}"})
        return json.loads(data["choices"][0]["message"]["content"]), f"mistral:{MISTRAL_MODEL}"
    except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError):
        return None, None


def _classify_with_llm(psp, dialect, raw_code, raw_message) -> Normalized:
    gemini_key = _env("GEMINI_KEY", "VITE_GEMINI_KEY")
    mistral_key = _env("MISTRAL_KEY", "VITE_MISTRAL_KEY")
    if not gemini_key and not mistral_key:
        return _fallback("no table entry and no LLM key configured "
                         "(set GEMINI_KEY or MISTRAL_KEY) -> safe default")

    prompt = _user_prompt(psp, dialect, raw_code, raw_message)
    out, provider = (_gemini(gemini_key, prompt) if gemini_key else (None, None))
    if out is None and mistral_key:
        out, provider = _mistral(mistral_key, prompt)
    if out is None:
        return _fallback("every configured LLM provider failed or timed out -> safe default")

    cls = out.get("error_class")
    if cls not in ERROR_CLASSES:
        # Hallucination gate: a class outside the enum would walk straight into
        # the retry machine's "unrecognized" branch. Refuse it here instead.
        return _fallback(f"{provider} returned '{cls}', which is not a valid error class "
                         "-> discarded, safe default")
    try:
        conf = float(out.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    if conf < MIN_CONFIDENCE:
        return _fallback(f"{provider} classified as '{cls}' but only at confidence "
                         f"{conf:.2f} (< {MIN_CONFIDENCE}) -> safe default")
    return Normalized(cls, conf, "llm", provider, str(out.get("reasoning", ""))[:400])


def normalize(psp: str, raw_code: str = None, raw_message: str = None) -> Normalized:
    """Map one PSP's raw decline onto orchestrator.ERROR_CLASSES."""
    cache_key = (psp, raw_code, raw_message)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    dialect = PSP_DIALECT.get(psp, DEFAULT_DIALECT)
    result = None

    if raw_code:
        hit = CODE_TABLE.get((dialect, _key(dialect, raw_code)))
        if hit:
            result = Normalized(hit, 1.0, "table", None,
                                f"deterministic map: {dialect} code '{raw_code}' -> {hit}")
    if result is None and raw_message:
        hit = (MESSAGE_TABLE.get((dialect, _key(dialect, raw_message)))
               or CODE_TABLE.get((dialect, _key(dialect, raw_message))))
        if hit:
            result = Normalized(hit, 1.0, "table", None,
                                f"deterministic map: {dialect} message '{raw_message}' -> {hit}")

    if result is None:
        if not raw_code and not raw_message:
            result = _fallback("no raw_code and no raw_message supplied -> safe default")
        else:
            result = _classify_with_llm(psp, dialect, raw_code, raw_message)

    _CACHE[cache_key] = result
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--psp", required=True, help="psp-a | psp-b | psp-c | psp-d")
    ap.add_argument("--code")
    ap.add_argument("--message")
    a = ap.parse_args()
    print(json.dumps(normalize(a.psp, a.code, a.message).to_dict(), indent=2))


if __name__ == "__main__":
    main()
