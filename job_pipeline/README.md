# Daily Job Application Pipeline

This directory implements the review-based pipeline for steady daily job
applications. It does not submit applications automatically. It scores jobs,
builds a daily queue, scaffolds application folders, and can sync with Google
Sheets once OAuth credentials are configured.

## Daily Workflow

1. Scan company career pages first, then LinkedIn saved searches, then public
   job boards.
2. Add new jobs to the tracker with status `found`.
3. Score the tracker and review the top 5.
4. Approve the jobs worth applying to today.
5. Scaffold the application folder, then use the existing JD tailoring workflow.
6. Submit manually and update the tracker.

## Quick Start With CSV

Create a local tracker:

```bash
python3 -m job_pipeline.cli init --csv /tmp/jobs.csv
```

Add a job:

```bash
python3 -m job_pipeline.cli new \
  --csv /tmp/jobs.csv \
  --company "Example Corp" \
  --role "Junior AI Engineer" \
  --country "Germany" \
  --city "Berlin" \
  --source "company career page" \
  --job-url "https://example.com/jobs/123" \
  --posted-date "2026-07-08" \
  --company-size "5000+"
```

Score and write tiers back to the tracker:

```bash
python3 -m job_pipeline.cli score --csv /tmp/jobs.csv --write
```

Print the daily top 5:

```bash
python3 -m job_pipeline.cli daily --csv /tmp/jobs.csv --limit 5
```

Scaffold an approved application folder from a visible spreadsheet row number:

```bash
python3 -m job_pipeline.cli prepare --csv /tmp/jobs.csv --row 2 --write
```

The first data row is row `2`, because row `1` is the CSV header.

Or scaffold the whole daily top 5 in one command:

```bash
python3 -m job_pipeline.cli prepare-daily --csv /tmp/jobs.csv --limit 5 --write
```

`prepare-daily` skips rows that already have an `application_folder`, so it is
safe to rerun after partial progress.

## Import Your Current Sheet Export

Your current Google Sheet export can stay compact:

```csv
Company,Job,Location,Piority(SABC),Date Applied,Status,Notes
```

Convert it into the pipeline schema:

```bash
python3 -m job_pipeline.cli import-legacy \
  --csv /path/to/company-list.csv \
  --out /private/tmp/company-list-pipeline.csv \
  --force
```

Then score and review:

```bash
python3 -m job_pipeline.cli score --csv /private/tmp/company-list-pipeline.csv --write
python3 -m job_pipeline.cli daily --csv /private/tmp/company-list-pipeline.csv --limit 5
```

The importer maps:

- `Company` -> `company`
- `Job` -> `role`
- `Location` -> `country` and `city` where it can infer them
- `Piority(SABC)` -> `tier`
- `Date Applied` -> preserved as `date_applied`
- `Status` -> `submitted`, `rejected`, `interview`, `offer`, or `found`
- `Notes` -> `result`, with source hints such as `email` and `LinkedIn Easy Apply`

## Google Sheets Sync

Install the optional Google client libraries:

```bash
python3 -m pip install -r job_pipeline/requirements-sheets.txt
```

Create an OAuth desktop credential in Google Cloud, save it as
`job_pipeline/credentials.json`, then pull your sheet:

```bash
python3 -m job_pipeline.cli sheets-pull \
  --spreadsheet-id "YOUR_SPREADSHEET_ID" \
  --range "Applications!A:U" \
  --out /tmp/jobs.csv
```

After scoring locally, push the CSV back:

```bash
python3 -m job_pipeline.cli sheets-push \
  --csv /tmp/jobs.csv \
  --spreadsheet-id "1CmpC7E2I0290bo3-VW7w6tYbVXgrBAVphkvQbJ6OVJA" \
  --range "Applications!A:U"
```

The first OAuth run opens a browser and writes `job_pipeline/token.json`.
Both credential files are ignored by git.

## Scoring Model

The score is deterministic and explainable. It combines:

- track fit: AIE/MLE vs SDE keywords
- company size, with `5000+` favored
- country and city priority
- source priority, with company career pages favored
- posted-date freshness
- seniority fit
- work-authorization risk for UK and Switzerland
- fully remote caution flags
- cover-letter effort

Tiers:

- `A`: deep CV tailoring, cover letter when useful, HR/hiring-manager follow-up
- `B`: fast keyword tailoring, skip cover letter unless required
- `C`: backlog or reject

## Guardrails

- Do not scrape LinkedIn or automate LinkedIn submissions.
- Use LinkedIn manually through saved searches, alerts, and messages.
- UK roles require no-photo CV format before submission.
- Every added-but-unknown CV skill must be logged in `skills-to-learn.md`.
- Application submission stays manual.
