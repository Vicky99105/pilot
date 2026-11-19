"""Standard exclusions Excl01 to Excl18 (IRDAI Guidelines on Standardization of Exclusions,
CIR/177/09/2019) and the Arogya Sanjeevani wording's specific exclusions.

Each row holds the code, a short title and a summary in our own words. The loader finds each
code in the circular and records its page; a code the circular does not show is reported and
not loaded. The wording is checked to carry all 18 codes (cross-check), and its specific
exclusions (numbered 19 to 23 in the Go Digit wording) are loaded as product rows with codes
SE19 to SE23.
"""

import re
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ExclusionRow
from app.knowledge.loaders.common import LoadReport, SourceFile, analyze
from app.knowledge.loaders.pdf import (
    NOT_STATED,
    ReferenceRelease,
    clear_release,
    lines_of,
    page_texts,
    percent,
    record_release,
    source_file,
    source_path,
)

CIRCULAR = "s3_irdai_exclusions/irdai_exclusions_guidelines_2019.pdf"
CIRCULAR_PAGES = range(5, 11)
WORDING = "s1_godigit_arogya_sanjeevani/arogya_sanjeevani_godigit.pdf"
WORDING_PAGES = range(8, 12)
WORDING_UIN = "GODHLIP24179V022324"
CROSS_CHECK = (
    "s2_newindia_arogya_sanjeevani/arogya_sanjeevani_newindia_policy_clauses.pdf",
    range(9, 14),
)
IRDAI = "IRDAI"
RELEASE = ReferenceRelease("exclusions", "CIR-177-09-2019", NOT_STATED)

# The code as printed: "Excl01", "Exel 12", "Exc115" (an l read as 1), "Code Excl18".
_CODE = re.compile(r"Code\s*[-\u2013]?\s*Ex[ce][l1I]\s*(\d)\s*(\d)?(?!\d)")

# (code, title, summary in our own words)
STANDARD: list[tuple[str, str, str]] = [
    (
        "Excl01",
        "Pre-existing diseases",
        "Treatment of a pre-existing disease and its direct complications is excluded until "
        "the policy's waiting period of continuous cover (at most 48 months) has passed. The "
        "wait starts again for any increase in sum insured, prior continuous cover under "
        "portability counts toward it, and after it the disease is covered only if it was "
        "declared and accepted.",
    ),
    (
        "Excl02",
        "Specified disease or procedure waiting period",
        "The conditions and procedures the policy lists are excluded until its waiting period "
        "of continuous cover (at most 48 months) has passed, unless caused by an accident. If "
        "the pre-existing disease wait also applies, the longer wait governs.",
    ),
    (
        "Excl03",
        "First 30 days",
        "Illness treated within 30 days of the first policy's start is excluded, except after "
        "an accident. The wait does not apply after more than 12 months of continuous cover, "
        "and applies afresh to any increase in sum insured.",
    ),
    (
        "Excl04",
        "Investigation and evaluation",
        "An admission mainly for diagnosis or evaluation is excluded, as are diagnostic costs "
        "not related or incidental to the current diagnosis and treatment.",
    ),
    (
        "Excl05",
        "Rest cure, rehabilitation and respite care",
        "An admission mainly for bed rest rather than treatment is excluded, including "
        "custodial or personal care and care that only supports a terminally ill person.",
    ),
    (
        "Excl06",
        "Obesity and weight control",
        "Surgery for obesity is excluded unless it is on a doctor's advice, backed by clinical "
        "protocols, for a person aged 18 or more, with a body mass index of 40 or more, or 35 "
        "or more with a listed severe co-morbidity after less invasive methods failed.",
    ),
    (
        "Excl07",
        "Change of gender",
        "Any treatment, surgical or not, to change the body's characteristics to those of the "
        "opposite sex is excluded.",
    ),
    (
        "Excl08",
        "Cosmetic or plastic surgery",
        "Treatment to change appearance is excluded unless it is reconstruction after an "
        "accident, burns or cancer, or a doctor certifies it as medically necessary to remove "
        "a direct and immediate health risk.",
    ),
    (
        "Excl09",
        "Hazardous or adventure sports",
        "Treatment needed because of taking part as a professional in hazardous or adventure "
        "sports is excluded.",
    ),
    (
        "Excl10",
        "Breach of law",
        "Treatment arising from the insured person committing or attempting a breach of law "
        "with criminal intent is excluded.",
    ),
    (
        "Excl11",
        "Excluded providers",
        "Treatment at a hospital or by a provider the insurer has excluded and published is not "
        "paid, except costs up to stabilisation in a life-threatening emergency after an "
        "accident.",
    ),
    (
        "Excl12",
        "Alcoholism, drug or substance abuse",
        "Treatment for alcoholism, drug or substance abuse or any addiction, and for what "
        "follows from them, is excluded.",
    ),
    (
        "Excl13",
        "Health hydros, nature cure and spas",
        "Treatment at health hydros, nature cure clinics, spas or similar places, or an "
        "admission arranged wholly or partly for domestic reasons, is excluded.",
    ),
    (
        "Excl14",
        "Dietary supplements",
        "Supplements and substances sold without prescription, such as vitamins and minerals, "
        "are excluded unless a doctor prescribed them as part of a hospitalisation or day care "
        "claim.",
    ),
    (
        "Excl15",
        "Refractive error",
        "Treatment to correct eyesight for a refractive error of less than 7.5 dioptres is "
        "excluded.",
    ),
    (
        "Excl16",
        "Unproven treatments",
        "Treatments, procedures and supplies without significant medical evidence of their "
        "effectiveness are excluded.",
    ),
    (
        "Excl17",
        "Sterility and infertility",
        "Sterilisation, assisted reproduction (such as IVF), gestational surrogacy and reversal "
        "of sterilisation are excluded; the circular also names birth control.",
    ),
    (
        "Excl18",
        "Maternity",
        "Childbirth costs, including complicated deliveries and caesarean sections, are "
        "excluded except for ectopic pregnancy, as are miscarriage (unless caused by an "
        "accident) and lawful termination of pregnancy.",
    ),
]

# The wording's specific exclusions: (code, number in the wording, words that identify it,
# title, summary in our own words).
SPECIFIC: list[tuple[str, int, str, str, str]] = [
    (
        "SE19",
        19,
        "war",
        "War and war-like events",
        "Illness or injury from war, invasion, hostilities, civil war, rebellion, military "
        "seizure or similar events is excluded.",
    ),
    (
        "SE20",
        20,
        "nuclear",
        "Nuclear, chemical or biological attack",
        "Loss from a nuclear, chemical or biological attack or weapon, alone or together with "
        "any other cause, is excluded.",
    ),
    (
        "SE21",
        21,
        "domiciliary",
        "Domiciliary and outpatient treatment",
        "Treatment at home (domiciliary hospitalisation) and outpatient treatment are not covered.",
    ),
    (
        "SE22",
        22,
        "outside the geographical limits",
        "Treatment outside India",
        "Treatment taken outside India is not covered.",
    ),
    (
        "SE23",
        23,
        "icd codes",
        "Disclosed diseases excluded by ICD code",
        "Where the schedule, with the insured's consent, excludes specified ICD codes for a "
        "disclosed existing disease, treatment under those codes is not covered.",
    ),
]


def find_codes(pages: Iterable[tuple[int, str]]) -> dict[str, int]:
    """Each Excl code found, with the first page it is printed on."""
    found: dict[str, int] = {}
    for page, text in pages:
        for match in _CODE.finditer(" ".join(lines_of(text))):
            digits = match[1] + (match[2] or "")
            code = f"Excl{int(digits):02d}"
            found.setdefault(code, page)
    return found


def find_specific(pages: Iterable[tuple[int, str]]) -> dict[str, int]:
    """The wording's specific exclusions found after its "SPECIFIC EXCLUSIONS" heading: each
    must start a line with its number and contain its identifying words."""
    found: dict[str, int] = {}
    started = False
    for page, text in pages:
        lines = lines_of(text)
        for index, line in enumerate(lines):
            if re.search(r"specific exclusions", line, re.IGNORECASE):
                started = True
                continue
            if not started:
                continue
            for code, number, words, _, _ in SPECIFIC:
                if code in found or not line.startswith(f"{number}. "):
                    continue
                window = " ".join(lines[index : index + 3]).lower()
                if words in window:
                    found[code] = page
    return found


async def load_exclusions(
    sessions: async_sessionmaker[AsyncSession], source_dir: Path
) -> LoadReport:
    circular = source_file(source_dir, CIRCULAR, "pp.5-10")
    wording = source_file(source_dir, WORDING, "pp.8-11 specific exclusions")
    circular_codes = find_codes(page_texts(source_path(source_dir, CIRCULAR), CIRCULAR_PAGES))
    wording_pages = list(page_texts(source_path(source_dir, WORDING), WORDING_PAGES))
    wording_codes = find_codes(wording_pages)
    specific = find_specific(wording_pages)
    report = LoadReport(system=RELEASE.system, version=RELEASE.version)
    files: list[SourceFile] = [circular, wording]

    standard = [(c, t, s) for c, t, s in STANDARD if c in circular_codes]
    async with sessions.begin() as session:
        await clear_release(session, RELEASE, ExclusionRow)
        for code, title, summary in standard:
            session.add(
                ExclusionRow(
                    scope=IRDAI,
                    code=code,
                    title=title,
                    summary=summary,
                    page=circular_codes[code],
                    source=RELEASE.system,
                    source_version=RELEASE.version,
                )
            )
        for code, _, _, title, summary in SPECIFIC:
            if code not in specific:
                continue
            session.add(
                ExclusionRow(
                    scope=WORDING_UIN,
                    code=code,
                    title=title,
                    summary=summary,
                    page=specific[code],
                    source=RELEASE.system,
                    source_version=RELEASE.version,
                )
            )
        circular.rows = len(standard)
        wording.rows = len(specific)
        cross_path, cross_pages = CROSS_CHECK
        if (source_dir / cross_path).is_file():
            cross = source_file(source_dir, cross_path, "pp.9-13 cross-check, not loaded")
            files.append(cross)
            cross_codes = find_codes(page_texts(source_dir / cross_path, cross_pages))
            report.checks["cross-check New India wording carries the 18 codes"] = percent(
                len(set(cross_codes) & set(circular_codes)), len(STANDARD)
            )
        record_release(session, RELEASE, files)
        await session.flush()
        await analyze(session, "exclusions", "kb_source_loads")

    expected = {code for code, _, _ in STANDARD}
    report.files = files
    report.counts = {"standard exclusions": len(standard), "specific exclusions": len(specific)}
    report.checks["standard codes not found in the circular"] = (
        ", ".join(sorted(expected - set(circular_codes))) or "none"
    )
    report.checks["cross-check Go Digit wording carries the 18 codes"] = percent(
        len(set(wording_codes) & expected), len(expected)
    )
    report.checks["specific exclusions not found in the wording"] = (
        ", ".join(c for c, *_ in SPECIFIC if c not in specific) or "none"
    )
    report.sample = [
        f"{IRDAI} {code} p{circular_codes[code]} {title}: {summary[:70]}..."
        for code, title, summary in standard
    ] + [f"{WORDING_UIN} {c} p{specific[c]} {t}" for c, _, _, t, _ in SPECIFIC if c in specific]
    report.sample = report.sample[:20]
    return report
