#!/usr/bin/env python3
"""
Gmail Organizer  -  generic, config-driven inbox organizer
==========================================================
End-to-end: AUDIT your inbox -> review/edit config.json -> APPLY (create
labels + filters via the Gmail API, and optionally label existing mail in
safe batches).

Why the API instead of importing filter XML:
  * Filter creation never silently "could not create" (works for keyword
    and attachment filters too, which the XML importer rejects).
  * Existing mail is labeled with users.messages.batchModify (1000 msgs per
    call) - fast and reliable at any scale, no import-time scan timeouts.

COMMANDS
  audit     Scan the account, write senders.csv + summary.txt (read-only).
  suggest   Read senders.csv, propose a category per uncategorized sender
            (heuristics in config), write suggested_categories.json to review.
  apply     Create labels + filters from config.json. With label_existing=true
            in config (or --label-existing), also tag existing mail.

SAFETY
  * dry_run defaults to TRUE in config: prints planned actions, changes nothing.
    Set "dry_run": false (or pass --go) to actually act.
  * archive_non_inbox defaults to FALSE: cautious, label-only.
  * inbox_only_labels never get archived even if archiving is on.

SETUP (one time)
  1. Same Google Cloud project as the audit. Add these scopes are fine via
     the consent screen automatically; you'll just re-authorize.
  2. pip install google-api-python-client google-auth-oauthlib google-auth-httplib2 httplib2
  3. credentials.json (Desktop OAuth client) next to this script.

RUN EXAMPLES
  python gmail_organizer.py audit
  python gmail_organizer.py suggest
  python gmail_organizer.py apply                 # dry run (from config)
  python gmail_organizer.py apply --go            # actually create filters+labels
  python gmail_organizer.py apply --go --label-existing --existing-limit 200   # safe test
  python gmail_organizer.py apply --go --label-existing --archive              # full, with archiving
"""

import os
import re
import csv
import sys
import json
import time
import argparse
from collections import Counter, defaultdict

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# modify -> read + label/archive existing mail ; settings.basic -> create filters
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]
CONFIG_PATH = "config.json"
CATEGORIES_PATH = "categories.json"     # account-specific mapping (you edit this)
SENDERS_CSV = "senders.csv"
SUMMARY_TXT = "summary.txt"
SUGGEST_JSON = "suggested_categories.json"

SYSTEM_LABELS = {"INBOX", "STARRED", "IMPORTANT", "SPAM", "TRASH", "UNREAD"}


# --------------------------------------------------------------------------- #
# Auth / service
# --------------------------------------------------------------------------- #
def get_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_categories(path, cfg):
    """Account-specific mapping. Prefer the categories file; fall back to
    config['categories'] only if the file is absent. Drops helper keys and
    the UNSORTED bucket."""
    cats = None
    src = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cats = json.load(f)
        src = path
    elif cfg.get("categories"):
        cats = cfg["categories"]
        src = "config.json"
    if not cats:
        sys.exit(
            f"No categories found.\n"
            f"  1) python gmail_organizer.py suggest   (writes {SUGGEST_JSON})\n"
            f"  2) review it, copy the categories you want into {CATEGORIES_PATH}\n"
            f"  3) python gmail_organizer.py apply\n"
            f"(Or start from the template: cp categories.example.json {CATEGORIES_PATH})"
        )
    # strip helper/comment keys and the UNSORTED bucket
    clean = {k: v for k, v in cats.items()
             if not k.startswith("_") and k.upper() != "UNSORTED"}
    if "UNSORTED" in cats:
        print(f"  (skipping 'UNSORTED' from {src} - sort those into real categories first)\n")
    return clean, src


# --------------------------------------------------------------------------- #
# Query / criteria builders (shared by filters and existing-mail search)
# --------------------------------------------------------------------------- #
def _or(values):
    return " OR ".join(values)


def build_search_query(cat):
    """Build a Gmail search string for a category definition."""
    if "raw_query" in cat:
        base = cat["raw_query"]
    else:
        parts = []
        if cat.get("from"):
            parts.append(f"from:({_or(cat['from'])})")
        if cat.get("subject_any"):
            quoted = [f'"{s}"' for s in cat["subject_any"]]
            parts.append(f"subject:({_or(quoted)})")
        if cat.get("from_not"):
            parts.append(f"-from:({_or(cat['from_not'])})")
        if cat.get("subject_not"):
            quoted = [f'"{s}"' for s in cat["subject_not"]]
            parts.append(f"-subject:({_or(quoted)})")
        base = " ".join(parts)
    return base.strip()


def build_filter_criteria(cat, bill_exclude_subject=None):
    """Map a category definition to Gmail API filter 'criteria'."""
    crit = {}
    pos_query = []   # goes into criteria.query
    neg_query = []   # goes into criteria.negatedQuery

    if "raw_query" in cat:
        pos_query.append(cat["raw_query"])
    else:
        if cat.get("from"):
            crit["from"] = _or(cat["from"])
        if cat.get("subject_any"):
            quoted = [f'"{s}"' for s in cat["subject_any"]]
            pos_query.append(f"subject:({_or(quoted)})")
        if cat.get("from_not"):
            neg_query.append(f"from:({_or(cat['from_not'])})")
        if cat.get("subject_not"):
            quoted = [f'"{s}"' for s in cat["subject_not"]]
            neg_query.append(f"subject:({_or(quoted)})")

    # Bill-exclusion: keep statements out of an archiving category
    if bill_exclude_subject:
        quoted = [f'"{s}"' for s in bill_exclude_subject]
        neg_query.append(f"subject:({_or(quoted)})")

    if pos_query:
        crit["query"] = " ".join(pos_query)
    if neg_query:
        crit["negatedQuery"] = " ".join(neg_query)
    return crit


# --------------------------------------------------------------------------- #
# Label management (idempotent, supports nesting via "/")
# --------------------------------------------------------------------------- #
def get_label_map(svc):
    resp = svc.users().labels().list(userId="me").execute()
    return {l["name"]: l["id"] for l in resp.get("labels", [])}


def ensure_label(svc, name, label_map, dry):
    """Create label (and parents) if missing. Returns label id (or None in dry)."""
    if name in label_map:
        return label_map[name]
    # ensure parents exist first (Gmail auto-handles, but be explicit/idempotent)
    if dry:
        print(f"    [dry] would create label: {name}")
        return None
    body = {"name": name, "labelListVisibility": "labelShow",
            "messageListVisibility": "show"}
    try:
        lbl = svc.users().labels().create(userId="me", body=body).execute()
        label_map[name] = lbl["id"]
        print(f"    created label: {name}")
        return lbl["id"]
    except HttpError as e:
        if e.resp.status == 409:  # already exists (race)
            label_map.update(get_label_map(svc))
            return label_map.get(name)
        raise


# --------------------------------------------------------------------------- #
# Filter management
# --------------------------------------------------------------------------- #
def existing_filters(svc):
    try:
        resp = svc.users().settings().filters().list(userId="me").execute()
        return resp.get("filter", [])
    except HttpError:
        return []


def filter_signature(criteria, action):
    return json.dumps({"c": criteria, "a": {
        "add": sorted(action.get("addLabelIds", [])),
        "rem": sorted(action.get("removeLabelIds", [])),
    }}, sort_keys=True)


def create_filter(svc, criteria, action, existing_sigs, dry):
    sig = filter_signature(criteria, action)
    if sig in existing_sigs:
        print("    (filter already exists, skipping)")
        return False
    if dry:
        print(f"    [dry] would create filter: criteria={criteria} add={action.get('addLabelIds')} remove={action.get('removeLabelIds')}")
        return False
    for attempt in range(5):
        try:
            svc.users().settings().filters().create(
                userId="me", body={"criteria": criteria, "action": action}
            ).execute()
            existing_sigs.add(sig)
            print("    filter created")
            return True
        except HttpError as e:
            if e.resp.status in (429, 500, 503):
                time.sleep(2 ** attempt)
            else:
                print(f"    ! filter error: {e}")
                return False
    return False


# --------------------------------------------------------------------------- #
# Existing-mail labeling (batchModify, 1000 ids/call)
# --------------------------------------------------------------------------- #
def label_existing_mail(svc, query, add_ids, remove_ids, cfg, dry, limit=0):
    settings = cfg["settings"]
    page_size = settings.get("list_page_size", 500)
    chunk = settings.get("batch_modify_size", 1000)
    sleep = settings.get("rate_limit_sleep", 1.0)

    # 1) collect matching message IDs
    ids, page = [], None
    cap = limit if limit > 0 else float("inf")
    while len(ids) < cap:
        resp = svc.users().messages().list(
            userId="me", q=query, maxResults=page_size, pageToken=page
        ).execute()
        msgs = resp.get("messages", [])
        if not msgs:
            break
        ids.extend(m["id"] for m in msgs)
        page = resp.get("nextPageToken")
        if not page:
            break
    if limit > 0:
        ids = ids[:limit]

    if not ids:
        print(f"    no existing messages match")
        return 0
    if dry:
        print(f"    [dry] would modify {len(ids)} existing messages "
              f"(add={add_ids} remove={remove_ids})")
        return len(ids)

    # 2) batchModify in chunks
    done = 0
    for i in range(0, len(ids), chunk):
        body = {"ids": ids[i:i + chunk], "addLabelIds": add_ids,
                "removeLabelIds": remove_ids}
        for attempt in range(5):
            try:
                svc.users().messages().batchModify(userId="me", body=body).execute()
                break
            except HttpError as e:
                if e.resp.status in (429, 500, 503):
                    time.sleep(sleep * (2 ** attempt))
                else:
                    print(f"    ! batchModify error: {e}")
                    break
        done += len(body["ids"])
        print(f"    modified {done}/{len(ids)}")
        time.sleep(sleep)
    return done


# --------------------------------------------------------------------------- #
# COMMAND: audit  (read-only sender scan)
# --------------------------------------------------------------------------- #
def parse_sender(h):
    if not h:
        return "", ""
    m = re.search(r"<([^>]+)>", h)
    if m:
        return m.group(1).strip().lower(), h[:m.start()].strip().strip('"')
    return h.strip().lower(), ""


def cmd_audit(svc, cfg):
    s = cfg["settings"]
    max_msgs = s.get("max_scan_messages", 0)
    include_st = s.get("include_spam_trash", False)
    print("Scanning (read-only)...")
    ids, page = [], None
    cap = max_msgs if max_msgs > 0 else float("inf")
    while len(ids) < cap:
        resp = svc.users().messages().list(
            userId="me", maxResults=500, pageToken=page,
            includeSpamTrash=include_st
        ).execute()
        msgs = resp.get("messages", [])
        if not msgs:
            break
        ids.extend(m["id"] for m in msgs)
        page = resp.get("nextPageToken")
        if len(ids) % 5000 < 500:
            print(f"  listed {len(ids)}")
        if not page:
            break
    if max_msgs > 0:
        ids = ids[:max_msgs]
    print(f"  {len(ids)} messages")

    sc, names, dc = Counter(), {}, Counter()
    for i in range(0, len(ids), 100):
        def cb(rid, resp, exc, _sc=sc, _names=names, _dc=dc):
            if exc:
                return
            hs = resp.get("payload", {}).get("headers", [])
            frm = next((h["value"] for h in hs if h["name"] == "From"), "")
            e, n = parse_sender(frm)
            if not e:
                return
            _sc[e] += 1
            if n and e not in _names:
                _names[e] = n
            _dc[e.split("@")[-1] if "@" in e else e] += 1
        batch = svc.new_batch_http_request(callback=cb)
        for mid in ids[i:i + 100]:
            batch.add(svc.users().messages().get(
                userId="me", id=mid, format="metadata", metadataHeaders=["From"]))
        for attempt in range(4):
            try:
                batch.execute(); break
            except (HttpError, OSError):
                time.sleep(2 ** attempt)
        if (i // 100) % 10 == 0:
            print(f"  scanned ~{min(i+100, len(ids))}/{len(ids)}")

    with open(SENDERS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["email", "display_name", "domain", "count"])
        for e, c in sc.most_common():
            w.writerow([e, names.get(e, ""), e.split("@")[-1] if "@" in e else e, c])
    with open(SUMMARY_TXT, "w", encoding="utf-8") as f:
        f.write(f"messages: {len(ids)}\nsenders: {len(sc)}\ndomains: {len(dc)}\n\nTOP 40 SENDERS\n")
        for e, c in sc.most_common(40):
            f.write(f"{c:6d}  {e}  ({names.get(e,'')})\n")
        f.write("\nTOP 40 DOMAINS\n")
        for d, c in dc.most_common(40):
            f.write(f"{c:6d}  {d}\n")
    print(f"Wrote {SENDERS_CSV} and {SUMMARY_TXT}")


# --------------------------------------------------------------------------- #
# Optional AI category suggestion (Claude / Gemini / OpenAI)
# Uses stdlib HTTP only. Sends sender DOMAINS + counts (no email content).
# API keys come from environment variables, never stored.
# --------------------------------------------------------------------------- #
import urllib.request
import urllib.error

AI_ENV = {"claude": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}

AI_PROMPT = (
    "You are organizing a Gmail inbox. Below is a list of email senders as "
    "'domain | message_count'. Group them into sensible Gmail "
    "label categories for inbox organization.\n\n"
    "Rules:\n"
    "- Return ONLY valid JSON, no prose, no markdown fences.\n"
    "- Format EXACTLY: {\"LabelName\": {\"from\": [\"domain1\", \"domain2\"]}, ...}\n"
    "- Use nested labels with '/' where helpful (e.g. \"Finance/Banking\", \"Career/Jobs\").\n"
    "- Put genuinely unknown or one-off senders under \"UNSORTED\".\n"
    "- Keep label names short and human-friendly.\n\n"
    "Senders:\n{senders}\n"
)


def _http_post_json(url, headers, body, timeout=120):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:500]
        raise RuntimeError(f"API HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e}")


def _ai_call(provider, model, key, prompt):
    if provider == "claude":
        resp = _http_post_json(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": key, "anthropic-version": "2023-06-01",
             "content-type": "application/json"},
            {"model": model, "max_tokens": 4000,
             "messages": [{"role": "user", "content": prompt}]},
        )
        return "".join(b.get("text", "") for b in resp.get("content", []))
    if provider == "gemini":
        resp = _http_post_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            {"content-type": "application/json"},
            {"contents": [{"parts": [{"text": prompt}]}]},
        )
        return resp["candidates"][0]["content"]["parts"][0]["text"]
    if provider == "openai":
        resp = _http_post_json(
            "https://api.openai.com/v1/chat/completions",
            {"Authorization": f"Bearer {key}", "content-type": "application/json"},
            {"model": model, "messages": [{"role": "user", "content": prompt}]},
        )
        return resp["choices"][0]["message"]["content"]
    raise ValueError(f"Unknown provider: {provider}")


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b != -1 and b > a:
            return json.loads(text[a:b + 1])
        raise


def ai_suggest(cfg, provider):
    """Group senders into categories using an LLM. Returns apply-ready dict."""
    ai = cfg.get("ai", {})
    if provider in (None, "config"):
        provider = ai.get("provider", "claude")
    if provider not in AI_ENV:
        sys.exit(f"Unknown AI provider '{provider}'. Use one of: {', '.join(AI_ENV)}")
    key = os.environ.get(AI_ENV[provider])
    if not key:
        sys.exit(f"Set your API key:  export {AI_ENV[provider]}=...   (provider: {provider})")
    model = ai.get("models", {}).get(provider, "")
    if not model:
        sys.exit(f"No model configured for {provider} in config.json -> ai.models")

    # Aggregate senders by domain (top N), send only domain + count (no names/content)
    rows = list(csv.DictReader(open(SENDERS_CSV, encoding="utf-8")))
    agg = {}
    for r in rows:
        d = r["domain"].lower()
        if not d:
            continue
        agg[d] = agg.get(d, 0) + int(r["count"])
    top = sorted(agg.items(), key=lambda x: -x[1])[: ai.get("max_senders", 300)]
    lines = "\n".join(f"{d} | {c}" for d, c in top)

    print(f"Asking {provider} ({model}) to categorize {len(top)} domains...")
    text = _ai_call(provider, model, key, AI_PROMPT.replace("{senders}", lines))
    data = _extract_json(text)
    # normalize: ensure {label: {"from": [...]}}
    out = {}
    for label, val in data.items():
        if isinstance(val, dict) and "from" in val:
            doms = val["from"]
        elif isinstance(val, list):
            doms = val
        else:
            continue
        out[label] = {"from": [str(x).lower() for x in doms if x]}
    return out


# --------------------------------------------------------------------------- #
# COMMAND: suggest  (propose categories for uncategorized senders)
# --------------------------------------------------------------------------- #
def cmd_suggest(cfg, ai_provider=None):
    if not os.path.exists(SENDERS_CSV):
        sys.exit(f"Run 'audit' first to create {SENDERS_CSV}")

    # AI path: let an LLM group senders (optional, opt-in)
    if ai_provider is not None:
        out = ai_suggest(cfg, ai_provider)
        with open(SUGGEST_JSON, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        n_un = len(out.get("UNSORTED", {}).get("from", []))
        total = sum(len(v.get("from", [])) for v in out.values())
        print(f"Wrote {SUGGEST_JSON}: {total} domains across {len(out)} categories "
              f"({n_un} UNSORTED).")
        print(f"Next: review it, save the categories you want as {CATEGORIES_PATH}, run 'apply'.")
        return

    # Heuristic path (no AI, no dependencies)
    mapped = set()
    for cat in cfg["categories"].values():
        for v in cat.get("from", []):
            mapped.add(v.lower())
    heur = cfg.get("suggest_heuristics", {})
    proposals = defaultdict(list)
    rows = list(csv.DictReader(open(SENDERS_CSV, encoding="utf-8")))
    for r in rows:
        e, d, c = r["email"].lower(), r["domain"].lower(), int(r["count"])
        if any(m in e or m in d for m in mapped) or "gmail.com" in d:
            continue
        chosen = None
        for cat, kws in heur.items():
            if any(k in e or k in d for k in kws):
                chosen = cat.replace("Travels2", "Travels")
                break
        proposals[chosen or "UNSORTED"].append({"domain": d, "count": c})
    # collapse to suggested category -> {"from": [domains]} (apply-ready), dedup domains
    out = {}
    for cat, items in proposals.items():
        seen, doms = set(), []
        for it in sorted(items, key=lambda x: -x["count"]):
            if it["domain"] not in seen:
                seen.add(it["domain"]); doms.append(it["domain"])
        out[cat] = {"from": doms}
    with open(SUGGEST_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    n_un = len(out.get("UNSORTED", {}).get("from", []))
    total = sum(len(v.get("from", [])) for v in out.values())
    print(f"Wrote {SUGGEST_JSON}: {total} new domains across {len(out)} buckets, "
          f"{n_un} UNSORTED.")
    print(f"Next: review it, then copy the categories you want into {CATEGORIES_PATH} "
          f"(rename/merge buckets, delete UNSORTED), and run 'apply'.")


# --------------------------------------------------------------------------- #
# COMMAND: apply  (create labels+filters, optionally label existing)
# --------------------------------------------------------------------------- #
def cmd_apply(svc, cfg, args):
    s = cfg["settings"]
    dry = s.get("dry_run", True) and not args.go
    create = s.get("create_filters", True)
    do_existing = s.get("label_existing", False) or args.label_existing
    archive_all = s.get("archive_non_inbox", False) or args.archive
    existing_limit = args.existing_limit if args.existing_limit is not None else s.get("existing_limit", 0)
    inbox_only = set(cfg.get("inbox_only_labels", []))
    bill = cfg.get("bill", {})
    bill_excl_cats = set(bill.get("exclude_from_categories", []))
    bill_subjects = bill.get("subject_any", [])

    print(f"\n{'='*60}\nMODE: {'DRY RUN (no changes)' if dry else 'LIVE'} | "
          f"create_filters={create} | label_existing={do_existing} | "
          f"archive_non_inbox={archive_all}\n{'='*60}\n")

    label_map = get_label_map(svc)
    sigs = {filter_signature(f.get("criteria", {}), f.get("action", {}))
            for f in existing_filters(svc)}

    # Account-specific mapping (NOT a hardcoded default)
    account_cats, src = load_categories(args.categories, cfg)
    print(f"Using {len(account_cats)} categories from {src}\n")

    # Build the full category list, injecting the generic Bill keeper if enabled
    categories = dict(account_cats)
    if bill.get("enabled", True) and bill.get("label"):
        categories[bill["label"]] = {
            "subject_any": bill.get("subject_any", []),
            "from_not": bill.get("from_not", []),
            "_star": bill.get("star", False),
            "_important": bill.get("mark_important", False),
            "_never_spam": bill.get("never_spam", False),
        }

    for label, cat in categories.items():
        print(f"[{label}]")
        # 1) ensure label
        lid = ensure_label(svc, label, label_map, dry)

        # 2) build action
        add_ids = [lid] if lid else ["<labelId>"]
        if cat.get("_star"):
            add_ids.append("STARRED")
        if cat.get("_important"):
            add_ids.append("IMPORTANT")
        remove_ids = []
        if cat.get("_never_spam"):
            remove_ids.append("SPAM")
        archive_this = archive_all and (label not in inbox_only)
        if archive_this:
            remove_ids.append("INBOX")

        # 3) create filter (apply bill-exclusion if this category archives + is bill-bearing)
        if create:
            _is_bill_cat = any(label == c or label.startswith(c) for c in bill_excl_cats)
            excl = bill_subjects if (archive_this and _is_bill_cat) else None
            criteria = build_filter_criteria(cat, bill_exclude_subject=excl)
            if criteria:
                action = {"addLabelIds": add_ids}
                if remove_ids:
                    action["removeLabelIds"] = remove_ids
                create_filter(svc, criteria, action, sigs, dry)
            else:
                print("    (no criteria; skipping filter)")

        # 4) label existing mail
        if do_existing:
            query = build_search_query(cat)
            if query:
                ex_add = [lid] if lid else []
                if cat.get("_star"):
                    ex_add.append("STARRED")
                if cat.get("_important"):
                    ex_add.append("IMPORTANT")
                ex_remove = ["INBOX"] if archive_this else []
                if lid or dry:
                    label_existing_mail(svc, query, ex_add, ex_remove, cfg, dry,
                                        limit=existing_limit)
        print()

    print("Done." + ("  (dry run - nothing changed; pass --go to act)" if dry else ""))


# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="Generic config-driven Gmail organizer")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit", help="scan account -> senders.csv, summary.txt")
    sp = sub.add_parser("suggest", help="propose categories for new senders")
    sp.add_argument("--ai", nargs="?", const="config", default=None,
                    choices=["claude", "gemini", "openai", "config"],
                    help="use an LLM to group senders (default provider from config). "
                         "Needs ANTHROPIC_API_KEY / GEMINI_API_KEY / OPENAI_API_KEY in env.")
    ap = sub.add_parser("apply", help="create labels+filters, optionally label existing")
    ap.add_argument("--go", action="store_true", help="actually act (override dry_run)")
    ap.add_argument("--label-existing", action="store_true", help="also tag existing mail")
    ap.add_argument("--archive", action="store_true", help="archive non-inbox categories")
    ap.add_argument("--existing-limit", type=int, default=None,
                    help="cap messages modified per category (safe test)")
    ap.add_argument("--categories", default=CATEGORIES_PATH,
                    help=f"account category file (default {CATEGORIES_PATH})")
    args = p.parse_args()

    cfg = load_config()
    if args.cmd == "suggest":
        cmd_suggest(cfg, args.ai); return
    svc = get_service()
    if args.cmd == "audit":
        cmd_audit(svc, cfg)
    elif args.cmd == "apply":
        cmd_apply(svc, cfg, args)


if __name__ == "__main__":
    main()
