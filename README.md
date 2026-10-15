# JobResearcher

JobResearcher collects posts from selected Telegram sources and turns them into a local, searchable job inbox. It keeps the original messages in SQLite, extracts fields from the post and, when a supported application link is present, can fetch the public job page. A local Ollama model handles the later classification and extraction step.

It is a personal research tool, not an application bot. Extracted fields and job-page status can be incomplete or wrong; check the original post and listing before acting on them.

## What it does today

- Collects new and edited messages from configured Telegram sources, and syncs recent history.
- Stores messages and job candidates in a local SQLite database.
- Parses common fields from message text, including links, contact details, location, salary, seniority, degree requirements, and skills.
- Fetches pages from an HTTPS host allowlist and extracts their main text.
- Uses Ollama locally to classify content and extract job fields. It can also look up exact ESCO occupation and skill candidates in a local catalog; it does not choose a final ESCO classification.
- Shows jobs and source messages in a Textual terminal interface. The inbox supports search, filters, sorting, and saved application statuses.
- Records multi-role and duplicate suggestions, plus consistency-audit results, for review. These checks do not merge or reject jobs automatically.

There is no match score in the current processing pipeline. Some older database fields and CLI code remain, but are not part of the workflow described here.

## How a post moves through the app

1. **Collection.** The Telegram collector listens for new and edited messages from enabled sources. It also syncs periodically, so a temporary disconnect does not rely on live events alone.
2. **Local parsing.** Each changed message is saved to the analysis queue. Deterministic parsers extract fields and links from its text; the original message remains available for review.
3. **Page retrieval.** If a candidate has an external URL, the fetcher checks the URL against its HTTPS host allowlist, follows only permitted redirects, and extracts page text. A failed or unsupported page does not erase the Telegram post.
4. **Enrichment.** Ollama classifies the post and extracts structured fields from the Telegram text and any fetched page. The code validates the JSON and checks that occupation and skill evidence appears in the supplied text. Missing or ambiguous fields remain unknown and may need review.
5. **Reference lookup and review.** Extracted occupation and skill terms are searched against the local ESCO catalog. Multi-role detection, duplicate suggestions, and consistency checks run as separate, non-destructive steps.
6. **Inbox.** The TUI shows job candidates that are classified as jobs and marked `enriched` or `needs_review`. Raw messages, including posts that are not jobs, remain available in the Messages view.

When the TUI starts, it starts the collector and processing pipeline in the background. The pipeline works through queued items and waits between checks; it is not a hosted service.

## Requirements and installation

- Python 3.11 or newer
- A Telegram account with access to the sources you want to monitor
- Telegram API credentials from [my.telegram.org](https://my.telegram.org/)
- Ollama running locally, with the model `qwen3.5:2b` available (the default model)

From the project root, create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

The current `pyproject.toml` does not declare every package imported by the TUI and enrichment code. Install those runtime imports as well:

```bash
python -m pip install textual django ollama
```

`django` is currently imported by the TUI even though the imported symbol is not used. The optional features below need additional packages.

## Configuration

Copy the example file and fill in your own values:

```bash
cp .env.example .env
```

Replace the placeholders with your credentials and source names:

```env
TELEGRAM_API_ID=YOUR_TELEGRAM_API_ID
TELEGRAM_API_HASH=YOUR_TELEGRAM_API_HASH
TELEGRAM_PHONE=YOUR_PHONE_NUMBER
TELEGRAM_CHANNELS=JOBS_CHANNEL,ANOTHER_JOBS_SOURCE
DB_PATH=jobresearcher.db
```

`TELEGRAM_CHANNELS` is a comma-separated list of source references used to match and enable account dialogs. The enabled state is stored in SQLite: removing a source from `.env` does not currently disable it if it was enabled on an earlier run. To stop collecting from such a source, disable its row in `telegram_sources` or use a fresh database. `DB_PATH` can be an absolute path; a relative path is resolved from the directory where you run the commands.

Keep `.env`, the generated Telegram session, and local databases private. The default database, session, and ESCO catalog are ignored by `.gitignore`; a custom `DB_PATH` may not be, so check before committing.

## First login and initial sync

Run these commands from the project root:

```bash
python -m scripts.login
python -m scripts.backfill --limit 200
```

The login command creates `jobresearcher_session.session` after Telegram authorization. On a source's first sync, `--limit` sets the maximum number of recent messages collected from that source. Later runs use the saved message cursor to sync newer messages; running the command again does not create duplicates for an already stored Telegram message.

Use an account dedicated to this project if possible, and add only the sources you intend to monitor. If the session file is exposed, revoke it from Telegram's **Settings → Devices**.

## Run the inbox

Make sure the Ollama service is running and the default model is present. If needed:

```bash
ollama pull qwen3.5:2b
```

Start the terminal app:

```bash
python -m jobresearcher.tui.app
```

The app creates or updates the SQLite schema, starts Telegram collection, and processes queued messages. You can also set `OLLAMA_MODEL` in the process environment to use a different model name; the default is `qwen3.5:2b`.

### Inbox keys

| Key | Action |
|---|---|
| `Enter` | Open the selected job or message |
| `/` | Focus the search field |
| `Esc` | Return from a job detail to the list |
| `j` / `m` | Show the Jobs inbox / Telegram messages |
| `r` | Refresh the current view |
| `u` | Cycle through user-status filters |
| `i` / `x` / `a` / `z` / `n` | Mark interested / rejected / applied / archived / new |
| `v` | Show or hide candidates marked as multi-job |
| `g` | Toggle the multi-job flag on the selected candidate |
| `d` | Include or hide candidates whose `duplicate_of` field is set |
| `s` | Cycle the sort order |
| `e` | Enrich the selected candidate again |
| `o` | Confirm and open the candidate's application or external URL |
| `Ctrl+R` | Reset inbox filters and search |
| `q` | Quit |

User application statuses are saved in SQLite. Search text, filters, and sort selection are in-memory UI state.

The search field accepts ordinary terms and field filters. For example:

```text
rust location:Europe work:remote seniority:mid degree:not_required
```

Supported filters are `location:<text>`, `work:remote|hybrid|onsite|unknown`, `remote:true`, `seniority:junior|mid|senior|lead|principal|unknown`, `contract:full_time|part_time|contract|internship|unknown`, `degree:required|preferred|not_required|unknown`, `salary:true|false`, and `page:true|false`. Every ordinary search term must match at least one searchable field. Use `work:onsite` or `work:hybrid` rather than `remote:false`.

## Useful batch commands

All commands run from the project root and use the configured `DB_PATH`.

```bash
# Analyze queued Telegram messages without starting the TUI
python -m scripts.analyze

# Fetch a batch of job pages; retries are included by default
python -m scripts.fetch_jobs --limit 10

# Normalize recognized URL formats
python -m scripts.fix_urls --limit 100

# Run local Qwen extraction for pending candidates
python -m jobresearcher.analysis.enrich --limit 10

# Save review suggestions and consistency checks
python -m scripts.detect_multi_jobs --limit 25
python -m scripts.detect_duplicates --limit 25
python -m scripts.audit_non_jobs --limit 25
```

`fetch_jobs` accepts `--no-retries` to skip retryable failures and `--browser` to enable a Playwright fallback for supported hosts. The browser fallback is not enabled in the TUI pipeline. To use it manually, install Playwright and its Chromium browser first:

```bash
python -m pip install playwright
python -m playwright install chromium
```

The fetcher only requests HTTPS URLs on its built-in allowlist. `JOB_ALLOWED_HOSTS` can add trusted hosts for HTTP fetching; browser fallback has its own narrower host list. A page marked open or closed is only a text-based signal, not a guarantee that the vacancy is still available.

## ESCO catalog

The ESCO SQLite catalog at `data/esco.sqlite3` is local and ignored by Git. Enrichment can still save extracted job data without it, but ESCO lookup will be unavailable until the catalog exists.

To build the catalog, provide the ESCO 1.2.0 RDF/XML export locally, then run:

```bash
python -m pip install rdflib
python -m jobresearcher.taxonomy.esco_importer --source esco-v1.2.0.rdf
```

The importer writes `data/esco.sqlite3` by default. The RDF/XML source is not included in this project. To search an existing catalog:

```bash
python -m jobresearcher.taxonomy.esco rust --kind skill
```

The lookup matches labels and aliases by exact phrase or prefix; it is not a fuzzy job classifier.

## Data and limitations

The message store, candidate data, and ESCO catalog are local SQLite files. Telegram collection requires a connection to Telegram, page fetching contacts the linked job site, and the Ollama client uses a local service by default. If you configure Ollama to use another host, job text is sent there; only do this with a service you trust.

The parser and model can misread posts, pages can disappear or block requests, and a channel post may not be a current vacancy. Review the source message and original listing before relying on extracted fields. The project does not bypass logins, CAPTCHAs, paywalls, or other access controls.
