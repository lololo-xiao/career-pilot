from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PIPELINE_DIR / "config.json"
DEFAULT_TEMPLATE = PIPELINE_DIR / "tracker-template.csv"
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y")

LEGACY_ALIASES = {
    "company": "company",
    "job": "role",
    "role": "role",
    "location": "location",
    "piority(sabc)": "tier",
    "priority(sabc)": "tier",
    "priority": "tier",
    "date applied": "date_applied",
    "posted date": "posted_date",
    "company size": "company_size",
    "job url": "job_url",
    "source": "source",
    "status": "status",
    "notes": "result",
}

CITY_COUNTRY = {
    "berlin": "Germany",
    "munich": "Germany",
    "munchen": "Germany",
    "muenchen": "Germany",
    "hamburg": "Germany",
    "frankfurt": "Germany",
    "frankurt": "Germany",
    "stuttgart": "Germany",
    "darmstadt": "Germany",
    "wuerzburg": "Germany",
    "wurzburg": "Germany",
    "saarbruecken": "Germany",
    "saarbrucken": "Germany",
    "amsterdam": "Netherlands",
    "dublin": "Ireland",
    "london": "United Kingdom",
    "uk": "United Kingdom",
    "milan": "Italy",
    "zurich": "Switzerland",
    "basel": "Switzerland",
    "barcelona": "Spain",
}


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def tracker_columns(config: dict[str, Any]) -> list[str]:
    return list(config["tracker"]["columns"])


def read_csv(path: Path, config: dict[str, Any]) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        raise SystemExit(f"CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit(f"CSV has no header row: {path}")
        fieldnames = list(reader.fieldnames)
        rows = []
        for row in reader:
            clean = {key: (value or "").strip() for key, value in row.items() if key is not None}
            for col in tracker_columns(config):
                clean.setdefault(col, "")
            rows.append(clean)
        return fieldnames, rows


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    extra_cols: list[str] = []
    for row in rows:
        for col in row:
            if col not in columns and col not in extra_cols:
                extra_cols.append(col)
    all_columns = columns + extra_cols
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=all_columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in all_columns})


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def ascii_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return normalize_text(ascii_value)


def combined_text(row: dict[str, str]) -> str:
    fields = [
        "company",
        "role",
        "country",
        "city",
        "source",
        "skills_added",
        "prep_gaps",
        "result",
        "job_description",
        "jd_text",
        "notes",
    ]
    return normalize_text(" ".join(row.get(field, "") for field in fields))


def parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    text = normalize_text(value)
    if "within 24" in text or text in {"today", "just posted", "new"}:
        return date.today()
    if "yesterday" in text:
        return date.today() - timedelta(days=1)
    week_match = re.search(r"(\d+)\s*week", text)
    if week_match:
        return date.today() - timedelta(days=int(week_match.group(1)) * 7)
    day_match = re.search(r"(\d+)\s*day", text)
    if day_match:
        return date.today() - timedelta(days=int(day_match.group(1)))
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def company_size_bucket(value: str) -> str:
    text = normalize_text(value)
    if not text:
        return "unknown"
    if any(
        token in text for token in ("5000+", "5,000+", "10000", "10,000", "large", "enterprise")
    ):
        return "5000_plus"

    numbers = []
    for match in re.finditer(r"(\d+(?:,\d{3})?)(\.\d+)?\s*k\b", text):
        whole = match.group(1).replace(",", "")
        decimal = match.group(2) or ""
        numbers.append(int(float(whole + decimal) * 1000))
    numbers.extend(int(match.replace(",", "")) for match in re.findall(r"\d[\d,]*", text))
    if numbers:
        high = max(numbers)
        if high >= 5000:
            return "5000_plus"
        if high >= 1000:
            return "1000_to_4999"
        if high >= 200:
            return "200_to_999"
        return "small"

    if any(token in text for token in ("startup", "small", "seed", "series a")):
        return "small"
    return "unknown"


def infer_location(location: str) -> tuple[str, str]:
    raw = (location or "").strip()
    key = ascii_key(raw)
    if not key:
        return "", ""
    if key in {"remote", "fully remote"}:
        return "", "Remote"

    countries = {
        country
        for city_key, country in CITY_COUNTRY.items()
        if re.search(rf"\b{re.escape(city_key)}\b", key)
    }
    if len(countries) == 1:
        return next(iter(countries)), raw
    if key in {"united kingdom", "great britain"}:
        return "United Kingdom", ""
    return "", raw


def legacy_status(value: str) -> str:
    status = normalize_text(value)
    if status in {"reject", "rejected", "no"}:
        return "rejected"
    if status in {"applied", "apply", "submitted"}:
        return "submitted"
    if status in {"interview", "oa", "online assessment"}:
        return "interview"
    if status in {"offer"}:
        return "offer"
    if status in {"found", "new"}:
        return "found"
    return status or "found"


def legacy_source(notes: str) -> str:
    text = normalize_text(notes)
    if "easy apply" in text:
        return "LinkedIn Easy Apply"
    if "email" in text:
        return "email"
    if "tracker" in text:
        return "tracker"
    return ""


def legacy_tier(value: str) -> str:
    tier = normalize_text(value).upper()
    if tier == "S":
        return "A"
    if tier in {"A", "B", "C"}:
        return tier
    return ""


def convert_legacy_row(row: dict[str, str], config: dict[str, Any]) -> dict[str, str]:
    converted = {col: "" for col in tracker_columns(config)}
    extras: dict[str, str] = {}

    for key, value in row.items():
        if key is None:
            continue
        alias = LEGACY_ALIASES.get(ascii_key(key), "")
        if alias == "company":
            converted["company"] = value.strip()
        elif alias == "role":
            converted["role"] = value.strip()
        elif alias == "location":
            country, city = infer_location(value)
            converted["country"] = country
            converted["city"] = city
        elif alias == "tier":
            converted["tier"] = legacy_tier(value)
            extras["original_priority"] = value.strip()
        elif alias == "date_applied":
            if normalize_text(value) not in {"empty", "none", "n/a", "na"}:
                extras["date_applied"] = value.strip()
        elif alias == "posted_date":
            converted["posted_date"] = value.strip()
        elif alias == "company_size":
            converted["company_size"] = value.strip()
        elif alias == "job_url":
            converted["job_url"] = value.strip()
        elif alias == "source":
            converted["source"] = value.strip()
        elif alias == "status":
            converted["status"] = legacy_status(value)
        elif alias == "result":
            converted["result"] = value.strip()
            converted["source"] = legacy_source(value)
        else:
            extras[key.strip()] = value.strip()

    if converted["status"] == "submitted" and not converted["follow_up_date"]:
        converted["next_action"] = (
            "Track response; follow up if this is a Tier A role and no reply after 7-10 days."
        )
    if converted["status"] == "rejected":
        converted["next_action"] = (
            "Archive; extract repeated gap if rejection included useful signal."
        )
    converted.update(extras)
    return converted


def keyword_score(text: str, keywords: list[str], points: int) -> tuple[int, list[str]]:
    hits = [keyword for keyword in keywords if keyword in text]
    return len(hits) * points, hits


def infer_track(row: dict[str, str], config: dict[str, Any]) -> tuple[str, int, list[str]]:
    existing = normalize_text(row.get("track", ""))
    text = combined_text(row)
    track_keywords = config["track_keywords"]

    ai_strong, ai_hits = keyword_score(text, track_keywords["ai_ml"]["strong"], 8)
    ai_adj, ai_adj_hits = keyword_score(text, track_keywords["ai_ml"]["adjacent"], 3)
    sde_strong, sde_hits = keyword_score(text, track_keywords["sde"]["strong"], 8)
    sde_adj, sde_adj_hits = keyword_score(text, track_keywords["sde"]["adjacent"], 3)

    ai_total = ai_strong + ai_adj
    sde_total = sde_strong + sde_adj

    if existing in {"ai", "ai/ml", "ai_ml", "aie", "mle", "ml"}:
        return "ai_ml", min(ai_total, 25), ai_hits + ai_adj_hits
    if existing in {"sde", "software", "backend", "backend/sde"}:
        return "sde", min(sde_total, 25), sde_hits + sde_adj_hits

    if ai_total >= sde_total:
        return "ai_ml", min(ai_total, 25), ai_hits + ai_adj_hits
    return "sde", min(sde_total, 25), sde_hits + sde_adj_hits


def source_bonus(row: dict[str, str], config: dict[str, Any]) -> tuple[int, str]:
    source = normalize_text(row.get("source", ""))
    if not source:
        return 0, "source unknown"
    for token, bonus in config["source_priority"].items():
        if token in source:
            return int(bonus), f"source +{bonus}: {token}"
    return 0, "source neutral"


def posted_bonus(row: dict[str, str]) -> tuple[int, str]:
    posted = parse_date(row.get("posted_date", ""))
    if not posted:
        return 0, "posted date unknown"
    age = (date.today() - posted).days
    if age < 0:
        return 0, "posted date is in the future"
    if age <= 1:
        return 8, "fresh posting <=1 day"
    if age <= 3:
        return 5, "fresh posting <=3 days"
    if age <= 7:
        return 3, "posted within 7 days"
    if age <= 14:
        return 0, "posted within 14 days"
    return -6, "old posting >14 days"


def deadline_bonus(row: dict[str, str]) -> tuple[int, str | None]:
    deadline = parse_date(row.get("deadline", ""))
    if not deadline:
        return 0, None
    days_left = (deadline - date.today()).days
    if days_left < 0:
        return -12, "deadline passed"
    if days_left <= 3:
        return 5, "deadline soon"
    return 0, None


def seniority_score(text: str, config: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0
    positive_hits = [kw for kw in config["positive_keywords"] if kw in text]
    risk_hits = [kw for kw in config["risk_keywords"] if kw in text]
    if positive_hits:
        score += min(14, len(positive_hits) * 5)
        reasons.append("early-career signal: " + ", ".join(positive_hits[:3]))
    if risk_hits:
        score -= min(35, len(risk_hits) * 12)
        reasons.append("seniority risk: " + ", ".join(risk_hits[:3]))
    if re.search(r"\b[3-4]\+?\s+years\b", text):
        score -= 10
        reasons.append("moderate years-of-experience risk")
    return score, reasons


def work_auth_score(row: dict[str, str], text: str, size_bucket: str) -> tuple[int, list[str]]:
    country = normalize_text(row.get("country", ""))
    reasons: list[str] = []
    score = 0
    large_company = size_bucket == "5000_plus"

    if "visa sponsorship not available" in text or "must already have the right to work" in text:
        score -= 35
        reasons.append("explicit sponsorship/work-rights risk")
    if country in {"uk", "united kingdom", "england", "scotland"}:
        score -= 24
        reasons.append("UK sponsorship needed")
        if large_company:
            score += 10
            reasons.append("large employer may sponsor")
    if country in {"switzerland", "swiss"}:
        score -= 18
        reasons.append("Switzerland work authorization risk")
        if large_company:
            score += 8
            reasons.append("large Swiss employer may sponsor")
    if "sponsor" in text or "visa" in text:
        score += 3
        reasons.append("sponsorship language present, verify manually")
    return score, reasons


def remote_score(text: str) -> tuple[int, str | None]:
    if any(
        token in text for token in ("fully remote", "remote only", "work from anywhere")
    ) and not any(token in text for token in ("office", "hybrid", "onsite", "on-site")):
        return -10, "fully remote without clear office"
    return 0, None


def cover_letter_effort(row: dict[str, str]) -> tuple[int, str | None]:
    value = normalize_text(row.get("cover_letter", ""))
    if ("required" in value or value == "yes") and "unless required" not in value:
        return -3, "cover letter required"
    return 0, None


def tier_for_score(score: int, config: dict[str, Any]) -> str:
    thresholds = config["tier_thresholds"]
    if score >= int(thresholds["A"]):
        return "A"
    if score >= int(thresholds["B"]):
        return "B"
    return "C"


def next_action_for(tier: str, row: dict[str, str]) -> str:
    cover = normalize_text(row.get("cover_letter", ""))
    cover_required = ("required" in cover or cover == "yes") and "unless required" not in cover
    if tier == "A":
        if cover_required:
            return "Deep tailor CV, draft cover letter, submit, then message HR/hiring manager."
        return "Deep tailor CV, submit, then message HR/hiring manager."
    if tier == "B":
        if cover_required:
            return "Keyword-tailor CV, write short required cover letter, submit if quick."
        return "Keyword-tailor CV, skip cover letter unless portal requires it."
    return "Backlog or reject unless there is a strong hidden reason."


def score_row(row: dict[str, str], config: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    text = combined_text(row)
    score = 25

    track, track_points, track_hits = infer_track(row, config)
    score += track_points
    if track_hits:
        reasons.append(f"{track} keyword fit: " + ", ".join(track_hits[:5]))
    else:
        reasons.append("weak keyword fit")

    country = row.get("country", "").strip()
    city = row.get("city", "").strip()
    country_points = int(config["country_priority"].get(country, 0))
    city_points = int(config["city_priority"].get(city, 0))
    score += country_points + city_points
    if country_points:
        reasons.append(f"country +{country_points}: {country}")
    if city_points:
        reasons.append(f"city +{city_points}: {city}")

    size_bucket = company_size_bucket(row.get("company_size", ""))
    size_points = int(config["company_size_bonus"].get(size_bucket, 0))
    score += size_points
    reasons.append(f"company size {size_bucket}: {size_points:+d}")

    src_points, src_reason = source_bonus(row, config)
    score += src_points
    reasons.append(src_reason)

    post_points, post_reason = posted_bonus(row)
    score += post_points
    reasons.append(f"{post_reason}: {post_points:+d}")

    deadline_points, deadline_reason = deadline_bonus(row)
    score += deadline_points
    if deadline_reason:
        reasons.append(f"{deadline_reason}: {deadline_points:+d}")

    seniority_points, seniority_reasons = seniority_score(text, config)
    score += seniority_points
    reasons.extend(seniority_reasons)

    auth_points, auth_reasons = work_auth_score(row, text, size_bucket)
    score += auth_points
    reasons.extend(auth_reasons)

    remote_points, remote_reason = remote_score(text)
    score += remote_points
    if remote_reason:
        reasons.append(f"{remote_reason}: {remote_points:+d}")

    effort_points, effort_reason = cover_letter_effort(row)
    score += effort_points
    if effort_reason:
        reasons.append(f"{effort_reason}: {effort_points:+d}")

    final_score = max(0, min(100, round(score)))
    tier = tier_for_score(final_score, config)
    return {
        "score": final_score,
        "tier": tier,
        "track": track,
        "cv_track": "master/cv/cv-ai-ml.tex" if track == "ai_ml" else "master/cv/cv-sde.tex",
        "next_action": next_action_for(tier, row),
        "reasons": reasons,
    }


def score_rows(
    rows: list[dict[str, str]], config: dict[str, Any], overwrite_status: bool = True
) -> None:
    for row in rows:
        status = normalize_text(row.get("status", ""))
        if status in {"submitted", "followed_up", "interview", "rejected", "offer"}:
            continue
        result = score_row(row, config)
        row["score"] = str(result["score"])
        row["tier"] = result["tier"]
        row["track"] = result["track"]
        row["cv_track"] = result["cv_track"]
        row["next_action"] = result["next_action"]
        if not row.get("prep_gaps"):
            row["prep_gaps"] = "; ".join(result["reasons"][:4])
        if overwrite_status and status in {"", "found", "backlog", "scored"}:
            row["status"] = "scored"
        if not row.get("cover_letter"):
            row["cover_letter"] = "recommended" if result["tier"] == "A" else "skip unless required"


def sort_key(row: dict[str, str]) -> tuple[int, int]:
    tier_rank = {"A": 3, "B": 2, "C": 1}.get(row.get("tier", ""), 0)
    try:
        score = int(row.get("score", "0"))
    except ValueError:
        score = 0
    return tier_rank, score


def active_rows(rows: list[dict[str, str]]) -> list[tuple[int, dict[str, str]]]:
    inactive = {"submitted", "followed_up", "interview", "rejected", "offer"}
    return [
        (idx, row)
        for idx, row in enumerate(rows, start=2)
        if normalize_text(row.get("status", "")) not in inactive
    ]


def print_daily(rows: list[dict[str, str]], limit: int) -> None:
    ranked = sorted(active_rows(rows), key=lambda item: sort_key(item[1]), reverse=True)
    print(f"# Daily review queue, top {limit}")
    print()
    print("| row | tier | score | track | company | role | city | next action |")
    print("|---:|---|---:|---|---|---|---|---|")
    for idx, row in ranked[:limit]:
        print(
            (
                "| {idx} | {tier} | {score} | {track} | {company} | {role} | {city} | {action} |"
            ).format(
                idx=idx,
                tier=row.get("tier", ""),
                score=row.get("score", ""),
                track=row.get("track", ""),
                company=row.get("company", ""),
                role=row.get("role", ""),
                city=row.get("city", ""),
                action=row.get("next_action", ""),
            )
        )


def slugify(value: str) -> str:
    value = normalize_text(value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def application_folder_for(row: dict[str, str]) -> Path:
    month = date.today().strftime("%Y-%m")
    company = slugify(row.get("company", "company"))
    role = slugify(row.get("role", "role"))
    return REPO_ROOT / "applications" / f"{month}-{company}-{role}"


def prepare_application(row: dict[str, str], dry_run: bool) -> Path:
    folder = application_folder_for(row)
    if dry_run:
        return folder
    if folder.exists():
        raise SystemExit(f"Application folder already exists: {folder}")
    folder.mkdir(parents=True)

    jd_text = row.get("job_description") or row.get("jd_text") or ""
    jd_body = [
        f"# {row.get('company', '').strip()} - {row.get('role', '').strip()}",
        "",
        f"- Source: {row.get('source', '').strip()}",
        f"- URL: {row.get('job_url', '').strip()}",
        f"- Country/city: {row.get('country', '').strip()} / {row.get('city', '').strip()}",
        f"- Pipeline score: {row.get('score', '').strip()} ({row.get('tier', '').strip()})",
        "",
        "## Job Description",
        "",
        jd_text.strip() or "TODO: paste the full JD here before tailoring.",
        "",
    ]
    (folder / "job-description.md").write_text("\n".join(jd_body), encoding="utf-8")

    cv_track = row.get("cv_track") or (
        "master/cv/cv-ai-ml.tex" if row.get("track") == "ai_ml" else "master/cv/cv-sde.tex"
    )
    source_cv = REPO_ROOT / cv_track
    if source_cv.exists():
        shutil.copyfile(source_cv, folder / "cv.tex")
    source_photo = REPO_ROOT / "master" / "photo.jpg"
    if source_photo.exists() and normalize_text(row.get("country", "")) not in {
        "uk",
        "united kingdom",
    }:
        shutil.copyfile(source_photo, folder / "photo.jpg")

    notes = [
        f"# Notes - {row.get('company', '').strip()} {row.get('role', '').strip()}",
        "",
        "## Pipeline decision",
        f"- Score: {row.get('score', '').strip()}",
        f"- Tier: {row.get('tier', '').strip()}",
        f"- Track: {row.get('track', '').strip()}",
        f"- Next action: {row.get('next_action', '').strip()}",
        "",
        "## Tailoring checklist",
        "- Save/verify the full JD above.",
        "- Run JD gap analysis against master/profile.md.",
        "- Tailor cv.tex from the selected master track.",
        "- Write cover-letter.md only if required or Tier A useful.",
        "- Log added-but-unknown skills in skills-to-learn.md.",
        "- Run ./build.sh on this folder and verify the CV is one page.",
        "",
        "## Guardrails",
        "- UK roles need no-photo format before submission.",
        "- Do not claim public InterviewAgent code or links.",
        "- Keep relocation line generic: Open to relocation.",
        "",
    ]
    (folder / "notes.md").write_text("\n".join(notes), encoding="utf-8")
    return folder


def cmd_init(args: argparse.Namespace) -> None:
    target = Path(args.csv)
    if target.exists() and not args.force:
        raise SystemExit(
            f"Refusing to overwrite existing CSV: {target}. Pass --force to replace it."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULT_TEMPLATE, target)
    print(f"Created tracker CSV: {target}")


def cmd_new(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    path = Path(args.csv)
    if not path.exists():
        write_csv(path, [], tracker_columns(config))
    columns, rows = read_csv(path, config)
    row = {col: "" for col in tracker_columns(config)}
    for field in (
        "company",
        "role",
        "country",
        "city",
        "source",
        "job_url",
        "posted_date",
        "deadline",
        "company_size",
    ):
        row[field] = getattr(args, field) or ""
    row["status"] = args.status
    rows.append(row)
    write_csv(path, rows, columns or tracker_columns(config))
    print(f"Added job row for {row['company']} - {row['role']}")


def cmd_import_legacy(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    source = Path(args.csv)
    out = Path(args.out)
    if out.exists() and not args.force:
        raise SystemExit(
            f"Refusing to overwrite existing output CSV: {out}. Pass --force to replace it."
        )
    with source.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit(f"CSV has no header row: {source}")
        rows = [
            convert_legacy_row({key: value or "" for key, value in row.items()}, config)
            for row in reader
        ]
    write_csv(out, rows, tracker_columns(config))
    print(f"Imported {len(rows)} rows into pipeline format: {out}")


def cmd_validate(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    fieldnames, rows = read_csv(Path(args.csv), config)
    missing = [col for col in tracker_columns(config) if col not in fieldnames]
    invalid_statuses = sorted(
        {
            row.get("status", "")
            for row in rows
            if row.get("status", "") and row.get("status", "") not in config["status_flow"]
        }
    )
    if missing:
        print("Missing columns:")
        for col in missing:
            print(f"- {col}")
    if invalid_statuses:
        print("Invalid statuses:")
        for status in invalid_statuses:
            print(f"- {status}")
    if not missing and not invalid_statuses:
        print(f"Tracker OK: {len(rows)} rows, {len(fieldnames)} columns.")


def cmd_score(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    fieldnames, rows = read_csv(Path(args.csv), config)
    score_rows(rows, config)
    out = Path(args.out) if args.out else Path(args.csv)
    if args.write or args.out:
        write_csv(out, rows, fieldnames)
        print(f"Wrote scored tracker: {out}")
    else:
        print_daily(rows, int(config["daily_application_target"]))
        print()
        print("Dry run only. Pass --write to update the CSV.")


def cmd_daily(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    _, rows = read_csv(Path(args.csv), config)
    score_rows(rows, config, overwrite_status=False)
    print_daily(rows, args.limit)


def cmd_prepare(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    fieldnames, rows = read_csv(Path(args.csv), config)
    score_rows(rows, config)
    data_index = args.row - 2
    if data_index < 0 or data_index >= len(rows):
        raise SystemExit(
            "--row uses the visible spreadsheet row number, so the first data row is 2."
        )
    row = rows[data_index]
    folder = prepare_application(row, dry_run=not args.write)
    if args.write:
        row["application_folder"] = str(folder.relative_to(REPO_ROOT))
        if normalize_text(row.get("status", "")) in {"", "found", "scored", "backlog"}:
            row["status"] = "approved"
        write_csv(Path(args.csv), rows, fieldnames)
        print(f"Prepared application folder: {folder}")
    else:
        print(f"Dry run. Would prepare: {folder}")
        print("Pass --write to create files and update the tracker.")


def cmd_prepare_daily(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    csv_path = Path(args.csv)
    fieldnames, rows = read_csv(csv_path, config)
    score_rows(rows, config)
    ranked = sorted(active_rows(rows), key=lambda item: sort_key(item[1]), reverse=True)

    prepared: list[tuple[int, Path, str]] = []
    skipped: list[tuple[int, str, str]] = []
    for row_number, row in ranked[: args.limit]:
        existing_folder = row.get("application_folder", "").strip()
        if existing_folder:
            skipped.append((row_number, row.get("company", ""), "already has application_folder"))
            continue
        folder = application_folder_for(row)
        if folder.exists():
            skipped.append((row_number, row.get("company", ""), "folder already exists"))
            if args.write:
                row["application_folder"] = str(folder.relative_to(REPO_ROOT))
                if normalize_text(row.get("status", "")) in {"", "found", "scored", "backlog"}:
                    row["status"] = "approved"
            continue
        folder = prepare_application(row, dry_run=not args.write)
        prepared.append((row_number, folder, row.get("company", "")))
        if args.write:
            row["application_folder"] = str(folder.relative_to(REPO_ROOT))
            if normalize_text(row.get("status", "")) in {"", "found", "scored", "backlog"}:
                row["status"] = "approved"

    if args.write:
        write_csv(csv_path, rows, fieldnames)

    action = "Prepared" if args.write else "Would prepare"
    for row_number, folder, company in prepared:
        print(f"{action} row {row_number} ({company}): {folder}")
    for row_number, company, reason in skipped:
        print(f"Skipped row {row_number} ({company}): {reason}")
    if not prepared and not skipped:
        print("No active jobs found for preparation.")
    if not args.write:
        print("Dry run only. Pass --write to create folders and update the tracker.")


def sheets_service(credentials: Path, token: Path):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise SystemExit(
            "Google client libraries are missing. Install them with:\n"
            "python3 -m pip install -r job_pipeline/requirements-sheets.txt"
        ) from exc

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials.exists():
                raise SystemExit(f"Missing Google OAuth credentials file: {credentials}")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials), scopes)
            creds = flow.run_local_server(port=0)
        token.write_text(creds.to_json(), encoding="utf-8")
    return build("sheets", "v4", credentials=creds)


def cmd_sheets_pull(args: argparse.Namespace) -> None:
    service = sheets_service(Path(args.credentials), Path(args.token))
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=args.spreadsheet_id, range=args.range)
        .execute()
    )
    values = result.get("values", [])
    if not values:
        raise SystemExit("No values returned from Google Sheets.")
    headers = values[0]
    rows = [
        dict(zip(headers, values_row + [""] * (len(headers) - len(values_row)), strict=True))
        for values_row in values[1:]
    ]
    write_csv(Path(args.out), rows, headers)
    print(f"Pulled {len(rows)} rows to {args.out}")


def cmd_sheets_push(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    fieldnames, rows = read_csv(Path(args.csv), config)
    service = sheets_service(Path(args.credentials), Path(args.token))
    values = [fieldnames] + [[row.get(col, "") for col in fieldnames] for row in rows]
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=args.spreadsheet_id,
            range=args.range,
            valueInputOption="RAW",
            body={"values": values},
        )
        .execute()
    )
    print(f"Pushed {len(rows)} rows to Google Sheets range {args.range}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Daily job application pipeline.")
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="Path to pipeline config JSON."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create a tracker CSV with the canonical header.")
    init.add_argument("--csv", required=True)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    new = sub.add_parser("new", help="Append one found job to a tracker CSV.")
    new.add_argument("--csv", required=True)
    new.add_argument("--company", required=True)
    new.add_argument("--role", required=True)
    new.add_argument("--country", default="")
    new.add_argument("--city", default="")
    new.add_argument("--source", default="")
    new.add_argument("--job-url", default="", dest="job_url")
    new.add_argument("--posted-date", default="", dest="posted_date")
    new.add_argument("--deadline", default="")
    new.add_argument("--company-size", default="", dest="company_size")
    new.add_argument("--status", default="found")
    new.set_defaults(func=cmd_new)

    import_legacy = sub.add_parser(
        "import-legacy",
        help="Convert the current compact Google Sheet export into pipeline CSV format.",
    )
    import_legacy.add_argument("--csv", required=True)
    import_legacy.add_argument("--out", required=True)
    import_legacy.add_argument("--force", action="store_true")
    import_legacy.set_defaults(func=cmd_import_legacy)

    validate = sub.add_parser("validate", help="Check tracker columns and statuses.")
    validate.add_argument("--csv", required=True)
    validate.set_defaults(func=cmd_validate)

    score = sub.add_parser("score", help="Score active jobs and assign tiers.")
    score.add_argument("--csv", required=True)
    score.add_argument("--out", default="")
    score.add_argument("--write", action="store_true")
    score.set_defaults(func=cmd_score)

    daily = sub.add_parser("daily", help="Print the daily top-N review queue.")
    daily.add_argument("--csv", required=True)
    daily.add_argument("--limit", type=int, default=5)
    daily.set_defaults(func=cmd_daily)

    prepare = sub.add_parser(
        "prepare", help="Create the application folder scaffold for a spreadsheet row."
    )
    prepare.add_argument("--csv", required=True)
    prepare.add_argument(
        "--row",
        type=int,
        required=True,
        help="Visible spreadsheet row number. First data row is 2.",
    )
    prepare.add_argument("--write", action="store_true")
    prepare.set_defaults(func=cmd_prepare)

    prepare_daily = sub.add_parser(
        "prepare-daily",
        help="Create application folder scaffolds for the current daily top-N queue.",
    )
    prepare_daily.add_argument("--csv", required=True)
    prepare_daily.add_argument("--limit", type=int, default=5)
    prepare_daily.add_argument("--write", action="store_true")
    prepare_daily.set_defaults(func=cmd_prepare_daily)

    pull = sub.add_parser("sheets-pull", help="Pull a Google Sheet range into CSV.")
    pull.add_argument("--spreadsheet-id", required=True)
    pull.add_argument("--range", required=True, help='Example: "Applications!A:U"')
    pull.add_argument("--out", required=True)
    pull.add_argument("--credentials", default="job_pipeline/credentials.json")
    pull.add_argument("--token", default="job_pipeline/token.json")
    pull.set_defaults(func=cmd_sheets_pull)

    push = sub.add_parser("sheets-push", help="Push a CSV back to a Google Sheet range.")
    push.add_argument("--csv", required=True)
    push.add_argument("--spreadsheet-id", required=True)
    push.add_argument("--range", required=True, help='Example: "Applications!A:U"')
    push.add_argument("--credentials", default="job_pipeline/credentials.json")
    push.add_argument("--token", default="job_pipeline/token.json")
    push.set_defaults(func=cmd_sheets_push)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
