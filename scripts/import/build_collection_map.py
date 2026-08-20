#!/usr/bin/env python3
"""Derive the Shamela collection → subject mapping automatically.

The sources carry 530 distinct `< مجموعة >` strings. They are not one string per
category: the same real category appears with orthographic variants (عربى/عربي),
separator variants (parentheses vs. a dash vs. "قسم"), appended language suffixes
(، فارسى / ، عربي), shortened wordings (اصول الفقه for either of two categories),
and — often — several real tags stacked with commas in one field.

The 39 subjects below are Shamela's own published category list (not a scheme we
invented), given directly by the project owner as ground truth. A subject slug is
therefore the single source of classification truth: there is no separate
tradition/madhhab/format dimension any more, because most of that distinction is
already encoded directly in which of the 39 a book falls under (e.g. فقه المذهب
الحنبلي already says fiqh + hanbali; مصادر العقائد عند السنيين already says aqaid +
sunni).

This applies ordered keyword rules to the *normalized* form of each string, using
the same normalizer the search index uses (which is what collapses عربى/عربي and
الشيعة/الشيعه), and emits a reviewable mapping plus an explicit list of anything it
could not classify confidently.

Rule order matters and encodes real distinctions found by reading every one of the
530 raw strings, largest book-count first (see needs_review for what's left):
رجال الحديث is not حديث; أصول الفقه is not فقه; فتاوى المراجع is not فقه; حديث ...
قسم الفقه is حديث, not فقه; a bare "مصادر الفقه" with no مذهب/سنة/شيعة qualifier is
the dedicated "مصادر فقهية مستقلة" category, not a guess at a school or tradition.

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

# Shamela appends the language to many collection names, sometimes twice ("، عربي ،
# فارسي") and sometimes as a whole extra tag ("عربي ، مصادر الحديث السنية ـ القسم
# العام"). Stripped from either end, repeatedly, before subject rules run — none of
# the 39 subjects are language-specific, so this text carries no classification signal.
LANGUAGE_TOKEN = r"عربي|عربى|فارسي|فارسى|انگليزي|انگليزى|انگليسي|انگليسى|فرانسوي|فرانسوى|آلماني|آلمانى"
LEADING_LANGUAGE = re.compile(rf"^\s*(?:{LANGUAGE_TOKEN})\s*[،,]\s*")
TRAILING_LANGUAGE = re.compile(rf"\s*[،,]\s*(?:{LANGUAGE_TOKEN})\s*$")
BARE_LANGUAGE_DASH = re.compile(rf"\s*[-ـ]\s*(?:{LANGUAGE_TOKEN})\s*$")


def _strip_language(normalized: str) -> tuple[str, str | None]:
    """Returns (text with language tags stripped, language_hint).

    language_hint is only ar/fa — the only two languages the corpus (and the
    `languages` table) actually has — and only set when every stripped tag agrees;
    a name carrying both ("، عربي ، فارسي") says nothing about any *one* book, since
    the per-file body marker is what actually decides a book's language.
    """
    languages: set[str] = set()
    changed = True
    while changed:
        changed = False
        for pattern in (TRAILING_LANGUAGE, LEADING_LANGUAGE, BARE_LANGUAGE_DASH):
            match = pattern.search(normalized)
            if match:
                if "فارس" in match.group(0):
                    languages.add("fa")
                elif "عرب" in match.group(0):
                    languages.add("ar")
                normalized = pattern.sub("", normalized).strip()
                changed = True
    hint = next(iter(languages)) if len(languages) == 1 else None
    return normalized, hint


# (regex on normalized, language-stripped text, subject slug). FIRST MATCH WINS.
#
# Ordered most-specific-first. Every regex here was checked against the real sorted
# collection list (530 entries, largest book-count first), not written from a
# top-of-head guess at what the words should mean.
SUBJECT_RULES: list[tuple[str, str]] = [
    # ── Rijal, before the generic حديث rule below ever gets a chance ──
    (r"رجال الحديث عند السنه|رجال الحديث.*السنه|رجال.*عند السنه",  "rijal-sunni"),
    (r"رجال الحديث عند الشيعه|رجال الحديث.*الشيعه|رجال.*عند الشيعه|مصادر الرجال", "rijal-shia"),

    # ── Usul al-fiqh, before the generic فقه rule ──
    (r"اصول.*فقه.*شيعه|اصول الفقه عند الشيعه",                    "usul-fiqh-shia"),
    (r"اصول.*فقه.*سنيه|اصول.*فقه.*المذاهب السنيه",                "usul-fiqh-sunni"),
    # Bare "اصول الفقه" / "مصادر الاصول" / "مصادر اصول الفقه" carries no tradition
    # marker at all. Of the two books this hits with any volume, all are Shia usul
    # works (Shamela's Sunni usul books are always tagged "...عند المذاهب السنية"
    # explicitly) -- confirmed by the fact every sunni raw variant above is
    # unambiguously tagged, unlike this bare fallback.
    (r"^اصول(?:.*فقه)?$|مصادر الاصول|مصادر اصول الفقه|مصادر اصول فقه", "usul-fiqh-shia"),

    # ── Fatawa / practical rulings framed as marja' fatwa -- Shia-specific per #29 ──
    (r"فقه الشيعه.*فتاوي المراجع|فتاوي المراجع",                   "fiqh-shia-fatawa"),

    # ── The two Shia-fiqh period buckets (#30/#31), before generic fiqh/madhhab ──
    (r"فقه الشيعه.*الي القرن الثامن|فقه الشيعه.*إلى القرن الثامن",  "fiqh-shia-qabl-thamin"),
    (r"فقه الشيعه من القرن الثامن",                                "fiqh-shia-bad-thamin"),

    # ── Madhhab-specific fiqh, before the generic فقه rule ──
    (r"مذهب.*حنبلي|فقه.*حنبلي",                                    "fiqh-hanbali"),
    (r"مذهب.*حنفي|فقه.*حنفي",                                      "fiqh-hanafi"),
    (r"مذهب.*زيدي|فقه.*زيدي",                                      "fiqh-zaydi"),
    (r"مذهب.*شافعي|فقه.*شافعي",                                    "fiqh-shafii"),
    (r"مذهب.*ظاهري|فقه.*ظاهري",                                    "fiqh-zahiri"),
    (r"مذهب.*مالكي|فقه.*مالكي",                                    "fiqh-maliki"),

    # ── Fiqh terminology / vocabulary, distinct from fiqh treatises themselves ──
    (r"مصطلحات.*فقهيه|مصطلحات ومفرده فقهيه",                       "mustalahat-fiqhiyya"),

    # ── Rulings framed generically (احكام شرعيه) fold into independent fiqh, not
    # into the marja'-fatwa bucket above -- that one requires "المراجع" explicitly ──
    (r"فقه استدلالي|فقه فتوايي|فقه فتوائي|احكام فقهيه",             "fiqh-mustaqilla"),

    # ── Hadith organized by fiqh topic ("قسم الفقه"), before generic حديث/فقه ──
    (r"الحديث السنيه.*فقه|السنيه.*قسم الفقه",                      "hadith-sunni-fiqh"),
    (r"الحديث الشيعيه.*فقه|الشيعيه.*قسم الفقه",                    "hadith-shia-fiqh"),
    (r"الحديث السنيه|حديث.*السنه.*عام|السنيه.*القسم العام",        "hadith-sunni-amm"),
    (r"الحديث الشيعيه|الشيعيه.*القسم العام",                       "hadith-shia-amm"),

    # ── Sects / heresiography, before the broader عقائد catch-all ──
    (r"الفرق والمذاهب|فرق و مذاهب|^فرق$",                          "firaq-madhahib"),

    # ── Aqaid, tradition-specific per #1/#2. Explicit polemical/salafi/convert
    # framings are strong tradition signals even without the word عقائد itself:
    # anti-Wahhabi refutations and "writings of converts" (مستبصرين) are Shia
    # apologetics; "Salafi aqaid" is definitionally Sunni. ──
    (r"عقائد.*(?:السنيين|السنه|سلفيه)|من مصادر العقائد عند السنيين|عقائد السلفيه", "aqaid-sunni"),
    (r"عقائد.*(?:الشيعه|الاماميه|الزيديه)|عقائد اهل الكتاب|وهابيه|مستبصرين|شبهات", "aqaid-shia"),

    # ── Rulings framed generically, not as marja' fatwa -- independent fiqh ──
    (r"الاحكام الشرعيه",                                            "fiqh-mustaqilla"),

    # ── Dream interpretation: checked before تفسير below purely because "تفسير
    # الاحلام" contains the word تفسير -- it is not exegesis. Closest real bucket
    # is the "other sciences" catch-all. ──
    (r"تفسير.*احلام",                                              "ulum-ukhra"),

    # ── Independent fiqh: the dedicated bucket for fiqh with no school/tradition
    # tag at all. Unanchored -- "مصادر الفقه" is the true value even when a second
    # tag (اصول الفقه, etc.) is stacked after it, and every more specific
    # school/tradition/fatwa wording was already checked above. ──
    (r"مصادر الفقه|^الفقه$|^فقه$|فقهيه مستقله",                     "fiqh-mustaqilla"),

    # ── Tafsir, tradition-specific per #10/#11 ──
    (r"تفسير عند السنه|تفسير.*السنه",                              "tafsir-sunni"),
    (r"تفسير عند الشيعه|تفسير.*الشيعه|تفسير.*الاماميه",            "tafsir-shia"),

    # ── Sira: single bucket, no tradition split in the 39. Unanchored: "مصادر
    # السيرة ، مصادر التاريخ" is a real stacked pair, and سيره is listed first. ──
    (r"سيره",                                                       "sira"),

    # ── History / geography: single bucket. Unanchored for the same reason --
    # "مصادر التاريخ" and "مصادر الجغرافيا" are themselves already the full raw
    # value in hundreds of books, just with a "مصادر" prefix the anchor missed. ──
    (r"التاريخ|الجغرافيا|تاريخ|جغرافيا",                            "tarikh-jughrafia"),

    # ── Genealogy / biographical dictionaries: single bucket ──
    (r"الانساب|معاجم مختلفه|التراجم|تراجم|اعلام",                   "ansab-tarajim"),

    # ── Devotional: single bucket ──
    (r"الادعيه|دعاء|زيارات|زيار|مزار|اداب الزياره",                  "adiya-ziyarat"),

    # ── Ethics + mysticism as ONE stacked label (فلسفه، منطق، عرفان together) is
    # Shamela's actual raw string for #24, not three separate tags -- checked
    # before the bare عرفان rule below, which is #34 instead. ──
    (r"فلسفه.*منطق.*عرفان|منطق.*فلسفه.*عرفان|فلسفه.*عرفان.*منطق",   "mantiq-falsafa"),
    (r"الاخلاق والعرفان|عرفان|اخلاق|تصوف",                          "akhlaq-irfan"),

    # ── Logic / philosophy left over (no عرفان/اخلاق -- those are checked above) ──
    (r"المنطق والفلسفه|فلسفه|منطق|حكمه|حكمت",                       "mantiq-falsafa"),

    # ── Arabic language / literature ──
    (r"علوم اللغه العربيه|مصادر اللغه|^لغه$|مصطلحات ومفرده(?! فقهيه)|ادب|نحو|صرف|بلاغه", "ulum-lugha"),

    # ── Poetry collections ──
    (r"دواوين الشعر|^دواوين$",                                     "dawawin-shir"),

    # ── Bibliographies / library indexes ──
    (r"دليل المؤلفات|فهارس المكاتب|دليل الكتب",                    "dalil-muallafat"),

    # ── Manuscripts ──
    (r"مخطوط",                                                     "makhtutat"),

    # ── Journals / miscellany ──
    (r"مجلات|^منوعات$|متفرقات",                                    "majallat-munawwaat"),

    # ── Medicine ──
    (r"طب",                                                        "tibb"),

    # ── Contemporary issues ──
    (r"قضايا اسلاميه ومعاصره|قضايا",                                "qadaya-muasira"),

    # ── Quran and its sciences ──
    (r"القران الكريم|علوم القران",                                 "quran-ulum"),

    # ── Everything else Shamela files under "other sciences": astronomy, science
    # vocabulary, pedagogy, mixed-topic Persian labels ──
    (r"علم النجوم|هييه|رياضيات|هندسه|كيمياء|تربيه|دانش|علوم$|گوناگون|مرتبط|متفرقه", "ulum-ukhra"),

    # ── Bare حديث / تفسير / عقائد with no tradition marker at all: no rule above
    # matched, and there is no neutral bucket for these three subjects in the 39 --
    # every hadith/tafsir/aqaid slot is tradition-specific. Quantified explicitly in
    # needs_review rather than guessed, since silently defaulting thousands of books
    # into the wrong tradition is worse than leaving them flagged. ──
]

# Detection only, never assignment to a real sunni/shia slot: these four subjects are
# tradition-specific in every one of the 39 (no neutral hadith/tafsir/aqaid/rijal
# bucket exists), so a raw string that only says "مصادر الحديث" with no سنة/شيعة
# marker genuinely does not say which of the two it is. Routed to the "other" catch-
# all (OTHER_SUBJECT) rather than a guessed tradition -- flagged via needs_review so
# it stays reviewable if a real signal ever resolves it.
AMBIGUOUS_TOPICS = (r"رجال", r"حديث", r"تفسير", r"عقائد")

# Not one of Shamela's 39 -- a 40th slug this project added so every book gets a real,
# non-NULL classification, per an explicit decision to prefer a labeled catch-all over
# leaving subject_id NULL.
OTHER_SUBJECT = "other"


def classify(raw: str) -> dict:
    normalized, language_hint = _strip_language(normalize(raw))
    base = {"raw": raw, "normalized": normalized, "language_hint": language_hint}

    matched = next((s for pattern, s in SUBJECT_RULES if re.search(pattern, normalized)), None)
    if matched is not None:
        return {**base, "subject": matched, "needs_review": False}

    if any(re.search(pattern, normalized) for pattern in AMBIGUOUS_TOPICS):
        return {**base, "subject": OTHER_SUBJECT, "needs_review": True}

    # True long-tail miscellany: nothing recognizable at all. This *is* what
    # majallat-munawwaat means, not a guess -- still flagged so it stays reviewable.
    return {**base, "subject": "majallat-munawwaat", "needs_review": True}


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
    mapped_books = total_books - sum(e["book_count"] for e in unmapped)

    args.out.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"collections: {len(entries)} distinct, {total_books:,} books")
    print(f"mapped     : {len(entries) - len(unmapped)} collections "
          f"({mapped_books:,} books, {100 * mapped_books / total_books:.1f}%)")
    print(f"for review : {len(unmapped)} collections "
          f"({sum(e['book_count'] for e in unmapped):,} books)")

    print("\n── subject distribution ──")
    by_subject: dict[str, int] = {}
    for e in entries:
        key = e["subject"] or "(uncategorized)"
        by_subject[key] = by_subject.get(key, 0) + e["book_count"]
    for subject, count in sorted(by_subject.items(), key=lambda kv: -kv[1]):
        print(f"  {count:6}  {subject}")

    if unmapped:
        print("\n── needs review, largest first ──")
        for e in sorted(unmapped, key=lambda e: -e["book_count"])[:60]:
            print(f"  {e['book_count']:5}  {e['raw']}")

    print(f"\nmapping → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
