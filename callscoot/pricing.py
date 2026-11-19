"""Pricing is the owner's call. The secretary never states, estimates,
negotiates or confirms a price, a discount, a quote or a shipping cost.

Three general checks, shared by the call path and the SMS path:

- asks_price(text): the caller or texter is asking about money. That turn gets
  the fixed hand-off line instead of a model reply.
- price_leak(text): a reply that names a money amount or states or negotiates
  a price. A model reply that trips it is replaced with the hand-off line.
- scrub(text): money amounts in retrieved context are removed before the model
  sees them, so it has no price to repeat.

Years, part numbers, phone numbers, hours and addresses are not money and pass.
"""
import re

from .config import dig

PRICE_REQUEST = "price_request"   # alert reason: the owner has a price to set

# spoken numbers (the call prompt has the model say numbers in words)
_NUM = (r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
        r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
        r"fifty|sixty|seventy|eighty|ninety|hundred|thousand|uno|dos|tres|cuatro|"
        r"cinco|seis|siete|ocho|nueve|diez|veinte|treinta|cuarenta|cincuenta|cien|"
        r"ciento|mil)")
_VAGUE = r"(?:a\s+few|a\s+couple(?:\s+of)?|few|couple(?:\s+of)?|several|some|a)"
_UNIT = r"(?:dollars?|bucks|usd|cents|d[oó]lares|pesos)"
# a figure that is not a year (1900-2099 stays: "2008 Ford", "listed for 1994 trucks")
_FIGURE = r"(?:(?!(?:19|20)\d\d\b)\d)"
# "three to five days" is a time, not a price
_NOT_TIME = r"(?![\w\s-]{0,24}\b(?:days?|weeks?|months?|hours?|minutes?|business)\b)"

# a money amount: "$40", "40 dollars", "forty-nine dollars", "a few bucks", "20% off"
MONEY_RE = re.compile(
    r"[$¢€£]\s?\d[\d,]*(?:\.\d+)?|"
    r"\b\d[\d,]*(?:\.\d+)?\s*-?\s*" + _UNIT + r"\b|"
    r"\b(?:" + _NUM + r"|" + _VAGUE + r")(?:[\s-]+(?:" + _NUM + r"|and|y))*[\s-]+" + _UNIT + r"\b|"
    r"\b\d+(?:\.\d+)?\s*(?:%|percent|por\s+ciento)\s+(?:off|discount|descuento)\b|"
    r"\bhalf\s+(?:off|price)\b",
    re.I)

# a reply stating or negotiating a price, with or without a unit
PRICE_TALK_RE = re.compile(
    r"\b(?:listed|priced|selling|asking|going)\s+at\b|\bpriced\b|"
    r"\b(?:going|selling|sells|sold|goes)\s+for\s+(?:only\s+|just\s+|about\s+|around\s+)?"
    r"(?:" + _FIGURE + r"|" + _NUM + r"\b)|"
    r"\b(?:price|cost|total|shipping|quote|estimate|fee|charge)s?\s*"
    r"(?:is|was|'s|would\s+be|will\s+be|comes\s+to|runs?|costs?)\s+"
    r"(?:only\s+|just\s+|about\s+|around\s+|roughly\s+|under\s+|over\s+|less\s+than\s+|more\s+than\s+)?"
    r"(?:" + _FIGURE + r"|" + _NUM + r"\b)" + _NOT_TIME + r"|"
    r"\bfree\s+shipping\b|\bships?\s+(?:for\s+)?free\b|\bshipping\s+(?:is\s+)?(?:included|free)\b|"
    r"\b(?:it'?ll|that'?ll|this'?ll|would|will)\s+(?:run|cost)\s+(?:you|about|around)\b|"
    r"\bcosts?\s+(?:about|around|roughly|under|over|less|more|only|just)\b|"
    r"\b(?:cheaper|less\s+expensive|more\s+expensive|lower\s+price|better\s+price|best\s+price)\b|"
    r"\bdiscount(?:s|ed)?\b|\bknock\s+(?:\w+\s+){0,2}(?:off|down)\b|"
    r"\b(?:give|get|offer|do|make)\s+you\s+a\s+(?:deal|discount|good\s+price|break)\b|"
    r"\b(?:accept|take)\s+(?:your|an?|that)\s+offer\b|\bbest\s+offer\b|\bobo\b|"
    r"\b(?:we|i|he|she|they)\s+(?:charge|pay|can\s+pay|would\s+pay|will\s+pay|can\s+do|could\s+do)\s+"
    r"(?:about\s+|around\s+)?(?:" + _FIGURE + r"|" + _NUM + r"\b)" + _NOT_TIME + r"|"
    r"\bcuesta(?:n)?\b|\bvale(?:n)?\s+\d|\bel\s+precio\s+es\b",
    re.I)

# the caller / texter is asking about money
PRICE_ASK_RE = re.compile(
    r"\bhow\s+much\b(?!\s+(?:longer|time|later|more\s+time|further|farther))|"
    r"\bprice[sd]?\b|\bpricing\b|\bcosts?\b|\bcosting\b|\bquotes?\b|\bestimates?\b|"
    r"\bwhat\s+(?:do|would|will|can|does)\s+(?:you|he|she|ya|it)\s+"
    r"(?:want|charge|get|ask|take|need|give|pay|go)\s+(?:for|me)\b|"
    r"\b(?:what|how\s+much)\b.{0,30}\b(?:pay|worth|charge)\b|"
    r"\b(?:do|does|would|will)\s+(?:you|he|she|it)\s+charge\b|"
    r"\byour\s+rates?\b|\brates?\s+for\b|"
    r"\b(?:discount|cheaper|best\s+offer|obo|negotiat\w*|haggle|wiggle\s+room)\b|"
    r"\b(?:a\s+)?(?:better|good|best)\s+deal\b|\bdeal\s+on\b|"
    r"\b(?:would|will|could)\s+(?:you|he|she)\s+(?:take|do|accept)\s+(?:\$|\d|" + _NUM + r"\b)|"
    r"\bshipping\s+(?:cost|price|fee|charge|rate)s?\b|\bfree\s+shipping\b|"
    r"\b(?:is|does)\s+(?:the\s+)?shipping\s+(?:free|included|extra)\b|\bships?\s+(?:for\s+)?free\b|"
    r"\bcharge\s+(?:for\s+)?shipping\b|"
    r"\bcu[aá]nto\b|\bprecio\b",
    re.I)


def asks_price(text):
    """True when this caller / texter turn asks about a price, a quote, a
    discount, what something is worth, or a shipping cost."""
    t = text or ""
    return bool(PRICE_ASK_RE.search(t) or MONEY_RE.search(t))


def price_leak(text):
    """The money amount or price phrase in a reply, or "" when it has none."""
    t = text or ""
    m = MONEY_RE.search(t) or PRICE_TALK_RE.search(t)
    return m.group(0) if m else ""


def scrub(text):
    """Retrieved context with every money amount taken out."""
    return MONEY_RE.sub("[price: owner only]", text or "")


def handoff_line(cfg, spoken=True):
    """What the secretary says instead of any price: spoken on calls, texted on SMS."""
    owner = dig(cfg, "persona.owner", "") or "The owner"
    if spoken:
        default = (f"{owner} sets every price, so I can't give one. Tell me the part, the "
                   f"year, make and model, and your name, and {owner} will call or text you "
                   f"back with a price.")
        return dig(cfg, "call.price_reply", "") or default
    default = (f"{owner} sets every price. Send the part, the year, make and model, and your "
               f"name, and {owner} will get back to you with a price.")
    return dig(cfg, "sms.price_reply", "") or default


def mask(number):
    """'+15705551234' -> '***1234' for logs: never the whole number."""
    d = re.sub(r"\D", "", str(number or ""))
    return ("***" + d[-4:]) if len(d) >= 4 else "***"


def log_block(cfg, where, number, hit, log=print):
    """One log line (journal + errors.log) for a reply that was replaced."""
    line = (f"price guard ({where}): reply to {mask(number)} named a price "
            f"({hit!r}) — sent the hand-off line instead")
    log(line)
    try:
        from . import errors
        errors.info(line, cfg)
    except Exception:
        pass
