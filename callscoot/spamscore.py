"""Scored robocall / telemarketer / scam evidence for calls and texts.

One plain-regex scorer, shared by the live call loop, the post-call robocall
gate and the SMS bot. It costs microseconds and no network.

How a verdict is reached:
  * each STRONG cue (a phrase that belongs to a sales or scam script) scores 2,
    each WEAK cue (a phrase customers also say) scores 1;
  * a cue counts once, however many times or turns it appears in;
  * CUSTOMER cues (a part, scrap/junk work, an order, "I need", "do you have")
    raise the bar.

  spam  = score >= 2 with no customer cue  (one scripted phrase, nothing else)
        or score >= 4 with a customer cue  (two script phrases outweigh it)

So a single weak phrase ("can you hear me", "act now", "donation") is never
enough, and one stray script phrase next to a real request is not enough
either: when unsure, the caller is treated as a customer.
"""
import re

_I = re.I

# phrase families that belong to sales / scam scripts (2 points each)
STRONG = {
    "warranty_pitch": re.compile(
        r"\b(vehicle|car|auto|automobile|truck|factory|extended|manufacturer)'?s?\W+"
        r"(\w+\W+){0,2}?warrant(y|ies|\s+he|\s+ee)?\b(?!\s+for\b)|"
        r"auto\s+protection|vehicle\s+service\s+contract", _I),
    "listing_pitch": re.compile(
        r"customers?\s+(cannot|can'?t|can\s+not|are\s+unable\s+to)\s+find\s+your\s+business|"
        r"\b(business|listing|profile)\b.{0,40}\b(not\s+(be\s+)?(displayed|showing|visible)|"
        r"unverified|be\s+verified|suspended|removed|expir\w*)|"
        r"google.{0,30}(verif\w*|unverified|first\s+page|rank\w*|expir\w*|removed|displayed|opt\s+out)|"
        r"first\s+page\s+of\s+google|search\s+engine\s+optimi|\bseo\b", _I),
    "opt_out": re.compile(
        r"\bopt[\s-]?out\b|to\s+be\s+removed\b|reply\s+stop\b|\bunsubscribe\b|"
        r"do[\s-]not[\s-]call\s+list", _I),
    "press_key": re.compile(
        r"\bpress\s+(\d|one|two|three|four|five|six|seven|eight|nine|zero|pound|star)\b", _I),
    "toll_free_callback": re.compile(
        r"\b(call|dial|text)\b.{0,15}\b\(?8(00|33|44|55|66|77|88)(?=[\s.)-]?\d)", _I),
    "automated": re.compile(
        r"pre-?recorded|recorded\s+message|automated\s+(message|call)|"
        r"this\s+is\s+an?\s+(important|final|courtesy)\s+(message|notice|call)|"
        r"this\s+(message|call)\s+is\s+(for|from|regarding)\b", _I),
    "government_threat": re.compile(
        r"\birs\b|internal\s+revenue|social\s+security.{0,60}(suspend|fraud|cancel|compromis)|"
        r"warrant.{0,15}arrest|lawsuit\s+(has\s+been\s+)?filed\s+against|"
        r"lawsuit\s+has\s+been\s+filed|legal\s+action\s+(will|has)", _I),
    "credit_offer": re.compile(
        r"credit\s+card.{0,25}(interest|rate|debt)|lower\s+your\s+(credit|interest|rate|payment)|"
        r"debt\s+(relief|consolidation|forgiveness)|student\s+loan|loan\s+forgiveness|"
        r"line\s+of\s+credit|business\s+(funding|loans?|capital)|working\s+capital|"
        r"capital\s+loan|no\s+collateral|"
        r"merchant\s+(processing|services|account)|processing\s+fees", _I),
    "you_qualify": re.compile(
        r"pre-?approved|you\s+(qualify|are\s+eligible)|you'?re\s+eligible", _I),
    "prize": re.compile(
        r"free\s+(vacation|cruise|gift|iphone|trip)|complimentary\s+(vacation|cruise|trip|stay)|"
        r"you('ve|\s+have)?\s+been\s+(selected|chosen)|(selected|chosen)\s+to\s+receive|"
        r"you('ve|\s+have)\s+won|gift\s+card|claim\s+your\s+(prize|reward)", _I),
    "consumer_program": re.compile(
        r"medicare|health\s+insurance\s+(market|plan)|solar\s+(panel|energy|program)|"
        r"are\s+you\s+the\s+homeowner|home\s+warranty|final\s+expense", _I),
    "survey_call": re.compile(
        r"political\s+(survey|poll)|quick\s+survey|"
        r"this\s+is\s+not\s+a\s+(sales|marketing)(\s+call)?", _I),
    "charity_ask": re.compile(
        r"on\s+behalf\s+of.{0,30}(charity|foundation|fund)\b|"
        r"(would|will|could)\s+you\s+(like\s+to\s+)?(make|give)\s+a\s+(donation|contribution)|"
        r"make\s+a\s+donation\s+today", _I),
    "business_pitch": re.compile(
        r"\b(we|i)\s+help\s+(\w+\s+){0,3}(businesses|companies|shops|owners)\b|"
        r"\bmarketing\s+(agency|services|company|team)|i'?m\s+with\s+\w+\s+marketing|"
        r"(message|calling)\s+(is\s+)?for\s+the\s+business\s+owner", _I),
    "more_leads": re.compile(
        r"(get|more|new)\s+(\w+\s+)?(leads|customers|reviews)\b", _I),
    "phishing": re.compile(
        r"confirm(ing)?\s+your\s+(identity|details|information|address|account)|"
        r"package\s+(is\s+)?(on\s+hold|could\s+not|was\s+not|is\s+pending)|"
        r"(click|tap)\s+(the|this)\s+link|at\s+the\s+link|verify\s+your\s+account|"
        r"account\s+(has\s+been\s+)?(suspended|locked)", _I),
}

# phrases customers also say (1 point each)
WEAK = {
    "can_you_hear_me": re.compile(r"can\s+you\s+hear\s+me", _I),
    "urgency": re.compile(
        r"act\s+now|call\s+now|limited\s+time|final\s+(notice|attempt|warning)|"
        r"\burgent\b|about\s+to\s+expire|today\s+only", _I),
    "giving": re.compile(r"\bcharity\b|\bdonation\b", _I),
    "congratulations": re.compile(r"\bcongratulations\b", _I),
    "reply_yes": re.compile(r"\breply\s+(yes|y)\b", _I),
    "trying_to_reach": re.compile(r"we'?ve\s+been\s+trying\s+to\s+(reach|contact)\s+you", _I),
}

# a real customer: wants a part, scrap/junk/e-waste work, or has an order
CUSTOMER = re.compile(
    r"\bi\s+(need|want|got|bought|ordered|purchased)\b|\bwe\s+need\b|"
    r"\bi\s+have\s+(a|some|this|that|my|old|two|three)\b|"
    r"\b(do|does|did)\s+(you|y'?all|he)\s+(guys\s+)?(have|carry|buy|take|sell|still)\b|"
    r"looking\s+for|\bmy\s+(order|package|truck|car|van|jeep)\b|\bebay\b|tracking\s+number|"
    r"\b(19|20)\d{2}\s+[a-z]|\b\d\.\d\b|"
    r"\b(starter|alternator|radiator|tailgate|bumper|fender|hood|mirror|headlight|"
    r"transmission|axle|rotor|caliper|gasket|fuel\s+pump|converter|core)s?\b",
    _I)

SPAM_MIN = 2              # score that flags spam when no customer cue is present
SPAM_MIN_CUSTOMER = 4     # score needed when the caller also sounds like a customer


def score(texts):
    """Score one text or a list of texts (all caller turns of one call).

    Returns {"score": int, "cues": [names], "customer": bool, "spam": bool}.
    """
    if isinstance(texts, str):
        texts = [texts]
    blob = " \n ".join(t for t in texts if t)
    cues, pts = [], 0
    for name, rx in STRONG.items():
        if rx.search(blob):
            cues.append(name)
            pts += 2
    for name, rx in WEAK.items():
        if rx.search(blob):
            cues.append(name)
            pts += 1
    from .agent import SALVAGE_RE  # late import: agent imports this module
    customer = bool(CUSTOMER.search(blob) or SALVAGE_RE.search(blob))
    need = SPAM_MIN_CUSTOMER if customer else SPAM_MIN
    return {"score": pts, "cues": cues, "customer": customer, "spam": pts >= need}


def is_spam(texts):
    return score(texts)["spam"]


def caller_turns(transcript):
    return [t for w, t in (transcript or []) if w == "caller" and t]
