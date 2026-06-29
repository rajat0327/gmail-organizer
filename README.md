# Gmail Organizer

A generic, config-driven command-line tool to organize any Gmail account:
**audit** your senders → **suggest** categories → edit `categories.json` → **apply**
(create labels + filters, and optionally label existing mail in safe batches).

It talks to the **Gmail API** directly instead of importing filter XML, so:
- filter creation never silently fails (keyword and attachment filters work too);
- existing mail is labeled with `batchModify` (up to 1000 messages per call),
  reliably even on very large mailboxes.

No personal data ships with this tool — you define your own categories.

## Features
- Audit any inbox into a ranked sender/domain report (read-only).
- Auto-suggest categories for unknown senders via editable keyword heuristics.
- Create nested labels + filters from a simple JSON mapping.
- Optionally back-apply labels (and archiving) to existing mail, in batches.
- A "never miss a bill" keeper: stars bill/statement mail and keeps it in the inbox.
- Dry-run by default; idempotent; safe to re-run.

## Setup
1. Create a Google Cloud project, enable the **Gmail API**, create an OAuth
   **Desktop** client, download it as `credentials.json` next to the script, and
   add your Google account under **Test users** on the consent screen.
2. Install dependencies:
   ```
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   ```

First run opens a browser to authorize. Scopes:
- `gmail.settings.basic` — create labels and filters
- `gmail.modify` — apply labels / archive existing mail (cannot permanently delete)

## Usage
```
python gmail_organizer.py audit        # read-only -> senders.csv, summary.txt
python gmail_organizer.py suggest       # -> suggested_categories.json (keyword guesses)

# OR let an LLM group your senders (optional):
python gmail_organizer.py suggest --ai            # uses the provider set in config.json
python gmail_organizer.py suggest --ai claude     # or: gemini / openai

# review suggested_categories.json: rename/merge buckets, DELETE the UNSORTED bucket,
# then save the categories you want as categories.json
# (or start from the template:  cp categories.example.json categories.json)

python gmail_organizer.py apply         # DRY RUN: prints planned actions, changes nothing
python gmail_organizer.py apply --go    # actually create labels + filters
```

### Labeling existing mail
```
python gmail_organizer.py apply --go --label-existing --existing-limit 200   # safe test
python gmail_organizer.py apply --go --label-existing                        # full, label-only
python gmail_organizer.py apply --go --label-existing --archive              # also skip the inbox
```

## AI category generation (optional)
`suggest` works with no API keys using keyword heuristics. If you'd rather have an
LLM group your senders, add `--ai`:
```
python gmail_organizer.py suggest --ai          # provider from config.json (default: claude)
python gmail_organizer.py suggest --ai gemini    # claude | gemini | openai
```
Set the matching API key in your environment first:
```
export ANTHROPIC_API_KEY=...   # for --ai claude
export GEMINI_API_KEY=...       # for --ai gemini
export OPENAI_API_KEY=...       # for --ai openai
```
- **Privacy:** only your sender **domains and message counts** are sent to the
  provider — never email content, subjects, sender names, or your address.
- Models are configurable under `ai.models` in `config.json`; update them as
  providers release new versions.
- Output is the same `suggested_categories.json` you review and save as
  `categories.json`. The AI never applies anything — you stay in control.
- Uses only the Python standard library (no extra dependencies for HTTP).

## `categories.json`
Your account-specific mapping. Each entry is `LabelName: { matcher }`:
```json
{
  "Work":        { "from": ["yourcompany.com", "github.com"] },
  "Finance":     { "from": ["paypal.com", "yourbank.com"] },
  "Tickets":     { "subject_any": ["PNR", "boarding pass"], "subject_not": ["offer", "sale"] },
  "Attachments": { "raw_query": "has:attachment -category:promotions -category:social" },
  "Me":          { "from": ["your-address@gmail.com"] }
}
```
Matchers (combine freely): `from`, `subject_any`, `subject_not`, `from_not`, `raw_query`.
Nested labels use `/` (e.g. `Career/LinkedIn`). A bucket named `UNSORTED` is ignored
by `apply`. See `categories.example.json` for a starting point.

## `config.json` (the engine)
- `settings` — `dry_run`, `create_filters`, `label_existing`, `archive_non_inbox`,
  `existing_limit`, batch sizes. CLI flags override these.
- `inbox_only_labels` — labels that never get archived, even with `--archive`.
- `bill` — generic bill/statement keeper (star + important + inbox). `enabled:false` to skip.
- `suggest_heuristics` — keyword→category guesses used by `suggest` (best-effort; you correct).
- `categories` — kept empty on purpose; your mapping lives in `categories.json`.

## Safety
- `dry_run: true` by default — nothing changes until you pass `--go`.
- `archive_non_inbox: false` by default — cautious, label-only first pass.
- Idempotent — re-running skips filters that already exist and re-applies labels harmlessly.
- Start with `--existing-limit` to sanity-check before processing a full mailbox.
- The included `.gitignore` keeps `credentials.json`, `token.json`, and your
  `categories.json` / audit output out of version control.

## How it works
`audit` lists message IDs and reads only `From` headers (metadata) to rank senders.
`apply` ensures each label exists, builds Gmail filter `criteria`/`action` from your
mapping, and creates the filters via the API. With `--label-existing`, it searches
each category's query and applies labels to matching messages with `batchModify`.

## License
MIT — do whatever you like; no warranty. 😆
