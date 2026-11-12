# Glenstar-Backend

Flask API for the Glenstar Intelligence dashboard.

**Every market statistic this API serves comes from a stored broker report and carries the page
and quote it was read from. There is no seed or sample data** — a fresh install is empty until
reports are added.

## How data gets in

1. **A document arrives** — a PDF dropped into the site, a pasted link to a PDF, or a PDF the
   scanner finds on a broker's page. Identical files are stored once.
2. **Claude reads it** (`extraction.py`) and returns the brokerage, market(s), quarter, and each
   statistic with the figure *exactly as printed*, a verbatim quote, and the page number. The
   quarter is taken only from the document, never from today's date.
3. **Code checks every value** (`verification.py`): the quote must appear in the PDF's text, the
   printed figure must be inside the quote, the stored number must equal the printed figure
   (allowing only unit scaling such as "1.2 MSF" → 1,200,000 SF), the sign must match, and the
   value must be plausible for that metric.
4. **A second, independent Claude pass audits each value** against the report.
5. A value is **published only if those checks pass**. Anything else is held as `needs_review`
   and is not served to the site until a person confirms, corrects or rejects it on the report
   page. Scanned PDFs with no text layer are always held for review.

Figures from different brokers are never averaged. Each market shows its newest quarter; older
quarters are kept as history.

## The scanner

Twice a day (06:00 and 18:00 UTC) the scanner visits the broker pages in `catalog.json`. It only
looks for documents — it never produces numbers itself. Most broker sites block automated
visitors; those are recorded as `blocked` and the market shows up under "needs a report" on the
site so the PDF can be dropped in by hand. Page addresses can be edited on the site's
*Broker sources* page.

## Quarters

`quarters.latest_published()` is the newest quarter that ended at least 21 days ago (brokers
publish roughly three to six weeks after quarter end). The site's coverage view compares each
tracked market against that quarter.

## Configuration

| Variable            | Required | Purpose |
| ------------------- | -------- | ------- |
| `ANTHROPIC_API_KEY` | Yes      | Reading reports, writing the thesis, Ask Claude. |
| `DATABASE_URL`      | Yes, for production | Postgres connection string. **Without it the app uses a local SQLite file, which on Render is wiped on every restart or deploy — uploaded reports would be lost.** |
| `ADMIN_TOKEN`       | Recommended | Passcode required to upload, edit, delete, scan or regenerate. If unset, anyone with the site address can change data. |
| `CLAUDE_MODEL`      | No       | Defaults to `claude-opus-5-5`. |

Run as **one process** (`--workers 1 --threads 8`, see `render.yaml` / `Procfile`): reports are
read in background threads and the scan is scheduled in-process.

Each report costs two Claude requests (read + audit). A thesis refresh is one more, triggered once
after a batch of uploads settles.

## API overview

| Endpoint | Purpose |
| -------- | ------- |
| `GET /api/stats` | Latest published quarter, coverage counts, setup status |
| `GET /api/markets` | Published figures per market, with sources, size bands and history |
| `GET /api/coverage` | Which tracked markets have a report for the latest quarter, and why not |
| `POST /api/documents` | Upload PDFs (multipart `files`) |
| `POST /api/documents/from-url` | Add a PDF by link |
| `GET /api/documents`, `/api/documents/<id>`, `/api/documents/<id>/file` | Library, detail with every value and its checks, original file |
| `PATCH /api/values/<id>` | Confirm, correct or reject a value |
| `POST /api/scan`, `GET /api/scan/status`, `GET /api/scan/targets` | Scanner |
| `GET /api/thesis/current`, `POST /api/thesis/regenerate` | AI thesis (built only from published values) |
| `POST /api/chat` | Ask Claude (answers only from published values) |

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests
python app.py            # http://localhost:5000
```

The tests run the real pipeline on generated PDFs with the Claude calls replaced by stand-ins,
and run the scanner against a simulated broker site (blocked, dead, PDF and empty pages).

## Earlier versions

Tables created by earlier versions of this app (`report`, `thesis`, `monitor_source`) are left in
the database but no longer read. Their contents were hard-coded sample figures, not report data.
