#!/usr/bin/env python3
"""Derive the Shamela collection → taxonomy mapping automatically.

The sources carry 530 distinct `< مجموعة >` strings. They are not a taxonomy: they mix
subject (التفسير), tradition (عند الشيعة / عند السنة), madhhab (فقه المذهب الشافعي),
format (مخطوطات، مجلات) and language (، فارسى) into one free-text field, with
orthographic and separator variants throughout.

This applies ordered keyword rules to the *normalized* form of each string — using the
same normalizer the search index uses, which is what collapses عربى/عربي and
الشيعة/الشيعه — and emits a reviewable mapping plus an explicit list of anything it could
not classify confidently.

Rule order matters and encodes real distinctions: رجال الحديث is rijal, not hadith;
أصول الفقه is usul-fiqh, not fiqh; فتاوى المراجع is rasail-amaliyya, not fiqh. The most
specific rule must be tried first.

Usage:
    python scripts/import/build_collection_map.py <scan-report.json> -o <mapping.json>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.arabic import normalize  # noqa: E402

# Shamela appends the language to many collection names. After normalization فارسى and
# فارسي are both "فارسي", which is precisely why this runs on normalized text. Some names
# carry two suffixes ("، عربي ، فارسي"), so this is applied repeatedly, not once.
LANGUAGE_SUFFIX = re.compile(r"\s*[،,]\s*(فارسي|عربي)\s*$")

# Schools of law whose name alone is enough to fix both tradition and madhhab. Zaydi
# fiqh follows its own school just as the five Sunni ones do -- omitting it left every
# Zaydi book with tradition=zaydi but madhhab=None, an inconsistency with how every
# other school is represented.
MADHAHIB = {
    "حنفي": "حنفي",
    "مالكي": "مالكي",
    "شافعي": "شافعي",
    "حنبلي": "حنبلي",
    "ظاهري": "ظاهري",
    "زيدي": "زيدي",
}

# (regex on normalized text, subject slug). FIRST MATCH WINS — order is meaningful.
SUBJECT_RULES: list[tuple[str, str]] = [
    # Before tafsir: "القرآن الكريم وعلومه" is Quranic sciences, not exegesis.
    (r"قران الكريم|علوم القران|تحريف قران",            "quran"),
    (r"رجال الحديث|رجال|درايه",                       "rijal"),
    (r"اصول الفقه|اصول فقه|مصادر الاصول|اصول",         "usul-fiqh"),
    # "احكام" not "احكام شرعيه": the normalized form is "الاحكام الشرعيه", so the
    # definite article sits between the two words and a two-word substring never matches.
    (r"فتاوي|رسائل عمليه|احكام",                       "rasail-amaliyya"),
    (r"فرق",                                           "aqaid"),
    # Before the generic fiqh rule: "مصادر الحديث ... ( قسم الفقه )" is a hadith
    # collection organized by fiqh topic, not a fiqh treatise -- confirmed by checking
    # every raw collection containing both words (419 books total across 4 variants).
    (r"حديث.*قسم.*فقه",                                "hadith"),
    (r"مذهب|فقه|فقهيه|فقهي",                          "fiqh"),
    (r"تفسير|علوم القران|القران الكريم",               "tafsir"),
    (r"حديث",                                         "hadith"),
    (r"عقائد|كلام|فرق والمذاهب|وهابيه|مستبصرين|اماميه|شبهات", "aqaid"),
    (r"سيره النبي|سيره|مقاتل|معصوم",                         "sira"),
    (r"تاريخ|جغرافيا",                                 "tarikh"),
    (r"انساب|تراجم|اعلام|معاجم مختلفه|طبقات",          "tarajim"),
    (r"ادعيه|دعاء|زيار|مزار",                          "adiya"),
    (r"اخلاق|عرفان|تصوف",                              "akhlaq"),
    (r"فلسفه|منطق|حكمه|حكمت",                               "falsafa"),
    (r"لغه|شعر|دواوين|بلاغه|ادب|نحو|صرف|مصطلحات",      "lugha"),
    (r"مؤلفات|فهارس|ببليوغرافيا|ذريعه",                "faharis"),
    (r"طب",                                            "tibb"),
    (r"نجوم|فلك|رياضيات|هندسه|كيميا|تربيه|دانش|علوم",       "ulum-ukhra"),
    (r"معاصر|قضايا",                                   "qadaya-muasira"),
    (r"مخطوط|منوعات|مجلات|متفرقات|مرتبط",              "munawwaat"),
]

FORMAT_RULES: list[tuple[str, str]] = [
    (r"مخطوط",                    "manuscript"),
    (r"مجلات|مجله",               "journal"),
    (r"دواوين",                   "diwan"),
    (r"مصطلحات|مفردات|معاجم",      "dictionary"),
    (r"دليل المؤلفات|فهارس",       "index"),
]


def classify(raw: str) -> dict:
    normalized = normalize(raw)

    languages: list[str] = []
    while match := LANGUAGE_SUFFIX.search(normalized):
        languages.append("fa" if "فارس" in match.group(1) else "ar")
        normalized = LANGUAGE_SUFFIX.sub("", normalized).strip()
    # A name tagged with both languages is a hint about the collection, not about any one
    # book, so it resolves to nothing; the per-file body marker decides each book anyway.
    language = languages[0] if len(set(languages)) == 1 else None

    # Tradition. Madhhab names imply Sunni even without the word السنه appearing.
    madhhab = next((v for k, v in MADHAHIB.items() if k in normalized), None)
    if "زيدي" in normalized:
        tradition = "zaydi"  # checked first: زيدي is now also in MADHAHIB, but must
                              # never fall through to the sunni branch below
    elif madhhab or re.search(r"السنه|السني|السنيين|المذاهب السنيه", normalized):
        tradition = "sunni"
    # "ائمه" (الأئمة/والأئمة): decided explicitly rather than guessed -- sīra collections
    # framed as "the Prophet AND the Imams" are Imami-specific wording, not shared by
    # Sunni sources. Checked every collection containing this word before adding it: one
    # already-sunni-tagged compound label is unaffected since the sunni branch above
    # already wins for it. "معصوم" (al-Ma'sumin, the Fourteen Infallibles) is exclusively
    # Twelver Shia doctrine -- checked, only 3 books, both unambiguous.
    elif re.search(r"الشيعه|الشيعيه|الاماميه|المراجع|المستبصرين|ائمه|معصوم", normalized):
        tradition = "shia"
    else:
        tradition = "shared"

    matched_subject = next(
        (s for pattern, s in SUBJECT_RULES if re.search(pattern, normalized)), None
    )
    # No rule matched: default to منوعات (miscellany) rather than leaving it NULL, per an
    # explicit decision that the long tail isn't worth hand-curating. needs_review stays
    # True so these remain findable if that decision is ever revisited.
    subject = matched_subject or "munawwaat"
    fmt = next((f for pattern, f in FORMAT_RULES if re.search(pattern, normalized)), "book")

    return {
        "raw": raw,
        "normalized": normalized,
        "subject": subject,
        "tradition": tradition,
        "madhhab": madhhab,
        "format": fmt,
        "language_hint": language,
        # "review" means a human should look, not that it is necessarily wrong: either the
        # subject was a defaulted fallback, or the format guess was manuscript.
        "needs_review": matched_subject is None or (fmt == "manuscript"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan_report", type=Path, help="output of scan_sources.py --json")
    parser.add_argument("-o", "--out", type=Path, required=True)
    args = parser.parse_args()

    report = json.loads(args.scan_report.read_text(encoding="utf-8"))
    counts: dict[str, int] = report["collections"]

    entries = []
    for raw, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        entry = classify(raw)
        entry["book_count"] = count
        entries.append(entry)

    total_books = sum(counts.values())
    unmapped = [e for e in entries if e["needs_review"]]
    review = unmapped
    mapped_books = total_books - sum(e["book_count"] for e in unmapped)

    args.out.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"collections: {len(entries)} distinct, {total_books:,} books")
    print(f"mapped     : {len(entries) - len(unmapped)} collections "
          f"({mapped_books:,} books, {100 * mapped_books / total_books:.1f}%)")
    print(f"unmapped   : {len(unmapped)} collections "
          f"({sum(e['book_count'] for e in unmapped):,} books)")
    print(f"for review : {len(review)} collections "
          f"({sum(e['book_count'] for e in review):,} books)")

    print("\n── subject distribution ──")
    by_subject: dict[str, int] = {}
    for e in entries:
        key = e["subject"] or "(unmapped)"
        by_subject[key] = by_subject.get(key, 0) + e["book_count"]
    for subject, count in sorted(by_subject.items(), key=lambda kv: -kv[1]):
        print(f"  {count:6}  {subject}")

    print("\n── tradition distribution ──")
    by_tradition: dict[str, int] = {}
    for e in entries:
        by_tradition[e["tradition"]] = by_tradition.get(e["tradition"], 0) + e["book_count"]
    for tradition, count in sorted(by_tradition.items(), key=lambda kv: -kv[1]):
        print(f"  {count:6}  {tradition}")

    if unmapped:
        print("\n── unmapped, largest first ──")
        for e in unmapped[:25]:
            print(f"  {e['book_count']:5}  {e['raw']}")

    print(f"\nmapping → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
