"""Load a source into the knowledge base. A one-off command; running it again replaces that
source version's rows in one transaction.

    python -m app.knowledge.load icd10cm|icd10pcs [--source-dir DIR]
    python -m app.knowledge.load non_payable|policy_wording|exclusions|cghs|hbp|medisep|drugs|stg
        [--source-dir DIR]

For the ICD loaders DIR holds the four CMS FY2027 archives (default .local/kb_sources/icd-2027).
For the public reference loaders DIR is the source inventory root (default .local/kb_sources),
laid out as MANIFEST.json describes. Prints each source file's SHA-256, the row counts,
consistency and cross-checks, and a 20-row spot-check sample.
"""

import argparse
import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.knowledge.loaders.cghs import load_cghs
from app.knowledge.loaders.common import DEFAULT_SOURCE_DIR, LoadReport
from app.knowledge.loaders.drugs import load_drugs
from app.knowledge.loaders.exclusions import load_exclusions
from app.knowledge.loaders.hbp import load_hbp
from app.knowledge.loaders.icd10cm import load_icd10cm
from app.knowledge.loaders.icd10pcs import load_icd10pcs
from app.knowledge.loaders.medisep import load_medisep
from app.knowledge.loaders.non_payable import load_non_payable
from app.knowledge.loaders.pdf import DEFAULT_REFERENCE_DIR
from app.knowledge.loaders.policy_wording import load_policy_wording
from app.knowledge.loaders.stg import load_stg

Loader = Callable[[async_sessionmaker[AsyncSession], Path], Awaitable[LoadReport]]
# name -> (loader, default source directory)
LOADERS: dict[str, tuple[Loader, Path]] = {
    "icd10cm": (load_icd10cm, DEFAULT_SOURCE_DIR),
    "icd10pcs": (load_icd10pcs, DEFAULT_SOURCE_DIR),
    "non_payable": (load_non_payable, DEFAULT_REFERENCE_DIR),
    "policy_wording": (load_policy_wording, DEFAULT_REFERENCE_DIR),
    "exclusions": (load_exclusions, DEFAULT_REFERENCE_DIR),
    "cghs": (load_cghs, DEFAULT_REFERENCE_DIR),
    "hbp": (load_hbp, DEFAULT_REFERENCE_DIR),
    "medisep": (load_medisep, DEFAULT_REFERENCE_DIR),
    "drugs": (load_drugs, DEFAULT_REFERENCE_DIR),
    "stg": (load_stg, DEFAULT_REFERENCE_DIR),  # after hbp: matches codes to HBP 2022
}


async def run(system: str, source_dir: Path | None = None) -> LoadReport:
    loader, default_dir = LOADERS[system]
    engine = create_async_engine(Settings().database_url)
    try:
        return await loader(
            async_sessionmaker(engine, expire_on_commit=False), source_dir or default_dir
        )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.knowledge.load")
    parser.add_argument("system", choices=sorted(LOADERS))
    parser.add_argument("--source-dir", type=Path, default=None)
    args = parser.parse_args()
    started = time.monotonic()
    report = asyncio.run(run(args.system, args.source_dir))
    print("\n".join(report.lines()))
    print(f"Done in {time.monotonic() - started:.1f} s")


if __name__ == "__main__":
    main()
