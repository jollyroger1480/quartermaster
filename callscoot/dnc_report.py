"""File one logged robocall / telemarketer call with the FTC Do Not Call complaint
form (https://www.donotcall.gov/report.html), the way that page's own JavaScript
(/js/report.js + /js/global.js, read 2026-11-18) does it. No browser.

    python3 -m callscoot.dnc_report                list records not yet reported
    python3 -m callscoot.dnc_report <id>           dry run: print the JSON + endpoints
    python3 -m callscoot.dnc_report <id> --send    file that one record

<id> is the call folder name (call_YYYYMMDD_HHMMSS_<number>), its full path, or
the record .md file name. One record per run; there is no batch mode.

The page's flow, reproduced here:
  1. step 1 checks (phone, date, robocall date range).
  2. Debt collection / survey subjects end on an info page: nothing is filed.
     Mobile text messages are sent to another site: never filed here.
  3. A live, daytime (08:00-20:59) call is first checked with POST
     /complaint-step2 {PhoneNumber, DateOfCall}: "0" goes on, "1" = your number
     is not registered, "2" = it is still in the 31-day grace period.
     A recorded or after-hours call skips that check.
  4. step 2 checks, then POST /save-complaint with the whole _model as JSON.
     {"message":"saved"} is success.

"phoneNumber" on that form is YOUR number (label: "What is your phone number, or
the phone number that received the call"). The number that called goes in
"companyPhoneNumber" ("Phone number that called"). Your number comes only from
phone.own_number in callscoot.toml (10 digits); without it nothing is sent. It
is the only personal value sent. A callback number the caller stated is added to
the comments. Name, address, city and zip stay empty, and state is sent
as "-", the value of the page's blank state choice.
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

from . import robocalls
from .config import dig, load

API_BASE = "https://www2.donotcall.gov"
STEP2_URL = API_BASE + "/complaint-step2"
SAVE_URL = API_BASE + "/save-complaint"
PAGE_ORIGIN = "https://www.donotcall.gov"
USER_AGENT = "Mozilla/5.0"
TIMEOUT_S = 30
REPORTED_NAME = "robocalls_reported.json"

# _model, in the key order report.js declares it.
MODEL_KEYS = ["phoneNumber", "dateOfCall", "timeOfCall", "minuteOfCall", "wasPrerecorded",
              "isMobileCall", "subjectMatter", "companyPhoneNumber", "companyName",
              "haveDoneBusiness", "askedToStop", "firstName", "lastName", "streetAddress1",
              "streetAddress2", "city", "state", "zip", "comments", "language", "validFlag"]

# ddlSubjectMatter (report.SubjectmatterContent in /js/i18n/en-US.json). The page
# sends $("#ddlSubjectMatter :selected").text(): the decoded label, double spaces kept.
SUBJECTS = {
    "3": "Reducing your debt (credit cards, mortgage, student loans)",
    "4": "Calls pretending to be government, businesses, or family and friends",
    "5": "Medical  & prescriptions",
    "6": "Home security  & alarms",
    "7": "Computer  & technical support",
    "8": "Debt collection",
    "9": "Energy, solar,  & utilities",
    "10": "Home improvement  & cleaning",
    "11": "Work from home  & other ways to make money",
    "12": "Warranties  & protection plans",
    "13": "Lotteries, prizes  & sweepstakes",
    "14": "Vacation  & timeshares",
    "15": "Surveys  & political calls",
    "16": "Charities",
    "2": "Dropped call or no message",
    "0": "Unknown",
    "1": "Other",
}
UNKNOWN_SUBJECT = "0"
NOT_FILED_SUBJECTS = {"8": "debt collection calls", "15": "survey and political calls"}

# First match wins, in this order. Plain keywords on the record summary.
SUBJECT_KEYWORDS = [
    ("8", r"debt collect|collection agency"),
    ("12", r"warrant|protection plan|vehicle service contract|service contract"),
    ("3", r"debt relief|credit card|interest rate|student loan|mortgage|consolidat|lower your debt"),
    ("5", r"medicare|medical|prescription|pharmac|health insurance|back brace|knee brace"),
    ("7", r"tech(nical)? support|computer|microsoft|virus|antivirus"),
    ("6", r"security system|home security|alarm"),
    ("9", r"solar|electric bill|utilit|energy"),
    ("10", r"home improvement|roof|gutter|window|siding|hvac|cleaning|duct"),
    ("11", r"work from home|make money|job offer|business opportunity"),
    ("13", r"prize|sweepstake|lotter|\bwon\b|gift card"),
    ("14", r"vacation|timeshare|cruise"),
    ("16", r"charit|donat"),
    ("15", r"survey|political|\bpoll"),
    ("4", r"social security|\birs\b|government|impersonat|pretend|amazon|apple support|"
          r"your bank|grandson|granddaughter|sheriff|police"),
    ("2", r"no message|dead air|silence|hung up immediately|dropped call"),
]

# /js/json/areaCodeStateMappings.json codes (US). The page refuses any other area code.
US_AREA_CODES = frozenset("""
201 202 203 205 206 207 208 209 210 212 213 214 215 216 217 218 219 220 223 224 225 227
228 229 231 234 235 239 240 248 251 252 253 254 256 260 262 267 269 270 272 274 276 279
281 283 301 302 303 304 305 307 308 309 310 312 313 314 315 316 317 318 319 320 321 323
324 325 326 327 329 330 331 332 334 336 337 339 340 341 346 347 350 351 352 353 357 360
361 363 364 369 380 385 386 401 402 404 405 406 407 408 409 410 412 413 414 415 417 419
423 424 425 430 432 434 435 436 440 442 443 445 447 448 457 458 463 464 465 469 470 471
472 475 478 479 480 483 484 500 501 502 503 504 505 507 508 509 510 512 513 515 516 517
518 520 530 531 534 539 540 541 551 557 559 561 562 563 564 567 570 571 572 573 574 575
580 582 585 586 601 602 603 605 606 607 608 609 610 612 614 615 616 617 618 619 620 621
623 624 626 628 629 630 631 636 640 641 645 646 650 651 656 657 659 660 661 662 667 669
670 671 678 679 680 681 682 684 686 689 701 702 703 704 706 707 708 712 713 714 715 716
717 718 719 720 724 725 726 727 728 729 730 731 732 734 737 738 740 743 747 748 754 757
760 762 763 765 769 770 771 772 773 774 775 779 781 785 786 787 800 801 802 803 804 805
806 808 810 812 813 814 815 816 817 818 820 821 826 828 830 831 832 833 835 837 838 839
840 843 844 845 847 848 850 854 855 856 857 858 859 860 861 862 863 864 865 866 870 872
877 878 888 901 903 904 906 907 908 909 910 912 913 914 915 916 917 918 919 920 924 925
928 929 930 931 934 936 937 938 939 940 941 943 945 947 948 949 951 952 954 956 959 970
971 972 973 975 978 979 980 983 984 985 986 989
""".split())

EARLIEST_CALL = datetime.date(2003, 10, 1)
EARLIEST_ROBOCALL = datetime.date(2009, 9, 1)
COMMENTS_MAX = 1000          # report.js: .substring(0, 1e3)
COMPANY_NAME_MAX = 60        # CallerNameTextBox maxlength
COMPANY_PHONE_MAX = 20       # CallerPhoneNumberTextBox maxlength
FILEABLE_VERDICTS = ("robocall", "telemarketer")


class DncError(Exception):
    """A record the form would not accept, or a refused run."""


# ---------- records ----------

def record_id(row):
    folder = (row.get("call_folder") or "").rstrip("/")
    if folder:
        return os.path.basename(folder)
    rec = row.get("record") or ""
    return os.path.splitext(os.path.basename(rec))[0]


def find_row(rows, key):
    key = (key or "").strip().rstrip("/")
    stem = os.path.splitext(os.path.basename(key))[0]
    for r in rows:
        if key and key in ((r.get("call_folder") or "").rstrip("/"), r.get("record") or ""):
            return r
        if stem and stem == record_id(r):
            return r
    return None


def reported_path(cfg):
    return os.path.join(robocalls.logs_dir(cfg), REPORTED_NAME)


def load_reported(cfg):
    try:
        with open(reported_path(cfg), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def mark_reported(cfg, rid, entry):
    data = load_reported(cfg)
    data[rid] = entry
    path = reported_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# ---------- field mapping ----------

def _digits(value):
    return re.sub(r"\D", "", str(value or ""))


def my_number(cfg):
    """phone.own_number only: the number that received the call."""
    d = _digits(dig(cfg, "phone.own_number", "") or "")
    return d[-10:] if len(d) == 11 and d.startswith("1") else d


def comments_for(row):
    """The record summary, plus the callback the caller stated, within the form's limit."""
    text = (row.get("summary") or "").strip()
    callback = " ".join((row.get("callback") or "").split())
    if callback:
        text = f"{text} Callback the caller stated: {callback}".strip()
    return text[:COMMENTS_MAX]


def pick_subject(summary):
    text = (summary or "").lower()
    for value, pattern in SUBJECT_KEYWORDS:
        if re.search(pattern, text):
            return value
    return UNKNOWN_SUBJECT


def _prerecorded(kind):
    kind = (kind or "").strip().lower()
    if kind == "prerecorded message":
        return "Y"
    if kind == "live telemarketer":
        return "N"
    return ""


def _company_phone(row):
    d = _digits(row.get("number"))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if re.fullmatch(r"\d{7,20}", d[:COMPANY_PHONE_MAX]) else ""


def my_name(cfg):
    """Optional phone.owner_first_name / phone.owner_last_name; blank when unset."""
    return tuple(" ".join(str(dig(cfg, f"phone.owner_{k}_name", "") or "").split())
                 for k in ("first", "last"))


def build_model(row, mine, name=("", "")):
    """The _model report.js would POST for this call."""
    date = datetime.datetime.strptime((row.get("date") or "").strip(), "%Y-%m-%d").date()
    clock = (row.get("time") or "").strip().split()[0] if (row.get("time") or "").strip() else ""
    hh, mm = clock.split(":")[:2]
    subject = pick_subject(row.get("summary"))
    asked = (row.get("dnc_requested") or "").strip().lower()
    model = dict.fromkeys(MODEL_KEYS, "")
    model.update({
        "phoneNumber": mine,
        "dateOfCall": date.strftime("%m/%d/%Y"),
        "timeOfCall": f"{int(hh):02d}",
        "minuteOfCall": f"{int(mm):02d}",
        "wasPrerecorded": _prerecorded(row.get("kind")),
        "isMobileCall": "",           # "Phone Call" radio; "Y" means a text message
        "subjectMatter": SUBJECTS[subject],
        "companyPhoneNumber": _company_phone(row),
        "companyName": " ".join((row.get("company") or "").split())[:COMPANY_NAME_MAX].strip(),
        "haveDoneBusiness": "",
        "askedToStop": "Y" if asked in ("yes", "y", "true", "1") else "",
        "firstName": name[0],
        "lastName": name[1],
        "state": "-",
        "comments": comments_for(row),
        "language": "en-US",
    })
    return model, subject


# ---------- the page's own checks ----------

def step1_errors(model, today=None):
    today = today or datetime.date.today()
    errs = []
    phone = model["phoneNumber"]
    if phone == "":
        errs.append("phone.own_number is missing in callscoot.toml: set the 10-digit number "
                    "that received the call.")
    elif not re.fullmatch(r"\d{10}", phone):
        errs.append("Your phone number must be a 10 digit number.")
    elif phone[:3] not in US_AREA_CODES:
        errs.append(f"Area code {phone[:3]} is not accepted by the form.")
    try:
        when = datetime.datetime.strptime(model["dateOfCall"], "%m/%d/%Y").date()
    except ValueError:
        return errs + ["The call date must be MM/DD/YYYY."]
    if when < EARLIEST_CALL or when > today:
        errs.append("The call date must be between 10/01/2003 and today.")
    elif model["wasPrerecorded"] == "Y" and when < EARLIEST_ROBOCALL:
        errs.append("A recorded-message call date must be between 09/01/2009 and today.")
    return errs


def step2_errors(model):
    errs = []
    cp = model["companyPhoneNumber"]
    if cp and not re.fullmatch(r"\d{7,20}", cp):
        errs.append("The company's phone number must be 7 to 20 digits.")
    for key in ("firstName", "lastName"):
        if model[key] and not re.fullmatch(r"[a-zA-Z- ]+", model[key]):
            errs.append(f"{key} may hold only letters, hyphens and spaces.")
    if model["zip"] and not re.fullmatch(r"\d{5}|\d{5}-\d{4}", model["zip"]):
        errs.append("Zip must be 12345 or 12345-6789.")
    if len(model["comments"]) > COMMENTS_MAX:
        errs.append("Comments are limited to 1000 characters.")
    return errs


def after_hours(model):
    h = int(model["timeOfCall"])
    return 0 <= h <= 7 or 21 <= h <= 23


def needs_step2_check(model):
    """report.js runs /complaint-step2 only for a live (not recorded), daytime call."""
    return model["wasPrerecorded"] != "Y" and not after_hours(model)


def plan(cfg, row, today=None):
    """(model, endpoints, problems). problems empty = the page would file it."""
    problems = []
    done = load_reported(cfg).get(record_id(row))
    if done:
        problems.append(f"already reported on {done.get('reported_at', '?')}")
    verdict = (row.get("verdict") or "").strip().lower()
    if verdict not in FILEABLE_VERDICTS:
        problems.append(f"verdict is {verdict or '(empty)'}, not robocall/telemarketer")
    model, subject = build_model(row, my_number(cfg), my_name(cfg))
    problems += step1_errors(model, today=today)
    if subject in NOT_FILED_SUBJECTS:
        problems.append(f"the form does not take {NOT_FILED_SUBJECTS[subject]} "
                        f"(subject '{SUBJECTS[subject]}' ends on an info page)")
    problems += step2_errors(model)
    endpoints = []
    if needs_step2_check(model):
        endpoints.append(("POST", STEP2_URL,
                          {"PhoneNumber": model["phoneNumber"], "DateOfCall": model["dateOfCall"]}))
    model["validFlag"] = "Y"
    endpoints.append(("POST", SAVE_URL, model))
    return model, endpoints, problems


# ---------- network (only with --send) ----------

def _post(url, payload, opener=None):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json;charset=utf-8",
        "User-Agent": USER_AGENT,
        "Origin": PAGE_ORIGIN,
        "Referer": PAGE_ORIGIN + "/report.html",
    })
    open_url = opener or urllib.request.urlopen
    with open_url(req, timeout=TIMEOUT_S) as resp:
        return resp.read().decode("utf-8", "replace")


STEP2_REFUSALS = {
    "1": "your number is not on the Do Not Call Registry, so the form will not take the complaint",
    "2": "your number is still in the registry's 31-day grace period",
}


def send(cfg, row, post=None, now=None, log=print):
    """File one record. Returns the parsed save-complaint reply. Raises DncError."""
    post = post or _post
    rid = record_id(row)
    if rid in load_reported(cfg):
        raise DncError(f"{rid} was already reported on {load_reported(cfg)[rid].get('reported_at')}")
    model, endpoints, problems = plan(cfg, row)
    if problems:
        raise DncError("not sent: " + "; ".join(problems))
    for _method, url, payload in endpoints:
        try:
            reply = post(url, payload)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise DncError(f"{url} failed: {e} (no retry)") from e
        log(f"{url} -> {reply.strip()[:500]}")
        if url == STEP2_URL:
            code = reply.strip().strip('"')
            if code in STEP2_REFUSALS:
                raise DncError("not sent: " + STEP2_REFUSALS[code])
            if code != "0":
                raise DncError(f"not sent: unexpected /complaint-step2 reply {reply.strip()[:200]!r}")
            continue
        try:
            data = json.loads(reply)
        except ValueError:
            raise DncError(f"save-complaint reply is not JSON: {reply.strip()[:200]!r}")
        if not (isinstance(data, dict) and data.get("message") == "saved"):
            raise DncError(f"not saved: {reply.strip()[:300]}")
        now = now or datetime.datetime.now()
        mark_reported(cfg, rid, {"reported_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                                 "call_date": row.get("date", ""), "call_time": row.get("time", ""),
                                 "number": row.get("number", ""), "reply": data})
        return data
    raise DncError("nothing to send")


# ---------- CLI ----------

def list_unreported(cfg):
    done = load_reported(cfg)
    out = []
    for r in sorted(robocalls.read_rows(cfg), key=lambda r: (r.get("date", ""), r.get("time", ""))):
        rid = record_id(r)
        if rid in done:
            continue
        note = "" if (r.get("verdict") or "").lower() in FILEABLE_VERDICTS else "  [not fileable]"
        company = f"  {r['company']}" if r.get("company") else ""
        out.append(f"{rid}  {r.get('date', '')} {r.get('time', '')}  {r.get('number', '')}  "
                   f"{r.get('verdict', '')}/{r.get('kind', '')}{company}{note}")
    return out


def dry_run_text(cfg, row):
    model, endpoints, problems = plan(cfg, row)
    lines = [f"record: {record_id(row)}", "DRY RUN: nothing sent.", "", "_model:",
             json.dumps(model, indent=2), "", "requests, in order:"]
    for i, (method, url, payload) in enumerate(endpoints, 1):
        what = "_model above" if url == SAVE_URL else json.dumps(payload)
        lines.append(f"  {i}. {method} {url}  {what}")
    if any(u == STEP2_URL for _m, u, _p in endpoints):
        lines.append("     (stops unless step 1 replies \"0\")")
    lines.append("")
    if problems:
        lines.append("would NOT be filed:")
        lines += [f"  - {p}" for p in problems]
    else:
        lines.append("ready: add --send to file this one record")
    return "\n".join(lines)


def main(argv=None, cfg=None, post=None):
    ap = argparse.ArgumentParser(prog="python3 -m callscoot.dnc_report",
                                 description="File one logged robocall with donotcall.gov.")
    ap.add_argument("record", nargs="?", help="call folder name, its path, or the record .md name")
    ap.add_argument("--send", action="store_true", help="actually file this one record")
    ap.add_argument("--config", help="callscoot.toml path")
    args = ap.parse_args(argv)
    cfg = cfg if cfg is not None else load(args.config)
    if not args.record:
        if args.send:
            print("--send needs one record id; there is no batch mode.", file=sys.stderr)
            return 2
        lines = list_unreported(cfg)
        print("\n".join(lines) if lines else "no unreported robocall records")
        return 0
    row = find_row(robocalls.read_rows(cfg), args.record)
    if row is None:
        print(f"no record {args.record!r} in {robocalls.csv_path(cfg)}", file=sys.stderr)
        return 1
    try:
        if not args.send:
            print(dry_run_text(cfg, row))
            return 0
        reply = send(cfg, row, post=post)
    except DncError as e:
        print(str(e), file=sys.stderr)
        return 1
    except (ValueError, KeyError, IndexError) as e:
        print(f"record {record_id(row)} has an unreadable date/time: {e}", file=sys.stderr)
        return 1
    print(f"saved: {json.dumps(reply)}; marked reported in {reported_path(cfg)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
