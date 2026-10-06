# stillhiring-swe

US software-engineering openings at the companies listed on [stillhiring.today](https://stillhiring.today/),
pulled from each company's own job board (the source of truth), not the sheet's tags.

- `scrape.py` — stdlib only. Reads the sheet (Airtable shared view), resolves each careers link to its ATS
  (Greenhouse, Ashby, Lever, Workday, SmartRecruiters, Recruitee, Breezy, Workable, BambooHR, Rippling, Gem,
  Teamtailor) via the URL, the page HTML, or a slug guess; keeps SWE titles with a US/unclear location.
- `docs/jobs.json` — deduped by job URL; `first_seen` drives the "new" filter; jobs vanish → `open:false`
  (only when that company's board fetched OK), dropped after 30 days.
- `docs/companies.json` — per-company status. "Needs manual check" in the UI = LinkedIn-only links, JS-only
  career sites, or unsupported ATS (iCIMS, Jobvite, UKG…).
- `docs/index.html` — the GUI (GitHub Pages). Applied/hide marks live in your browser only.
- `.github/workflows/scrape.yml` — runs every 4h, commits the JSON.

Tune the title/location filters at the top of `scrape.py`; `python3 scrape.py test` runs the self-check.
