"""Load a CMS ICD release into the knowledge base. A one-off command; running it again
replaces the release's rows in one transaction.

    python -m app.knowledge.load icd10cm [--source-dir DIR]
    python -m app.knowledge.load icd10pcs [--source-dir DIR]

DIR holds the four CMS FY2027 archives as downloaded (default .local/kb_sources/icd-2027).
Prints each source file's SHA-256, the row counts, consistency checks and a 20-row
spot-check sample.
"""

import argparse
import asyncio
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.knowledge.loaders.common import DEFAULT_SOURCE_DIR, LoadReport
from app.knowledge.loaders.icd10cm import load_icd10cm
from app.knowledge.loaders.icd10pcs import load_icd10pcs

LOADERS = {"icd10cm": load_icd10cm, "icd10pcs": load_icd10pcs}


async def run(system: str, source_dir: Path) -> LoadReport:
    engine = create_async_engine(Settings().database_url)
    try:
        return await LOADERS[system](async_sessionmaker(engine, expire_on_commit=False), source_dir)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.knowledge.load")
    parser.add_argument("system", choices=sorted(LOADERS))
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    args = parser.parse_args()
    started = time.monotonic()
    report = asyncio.run(run(args.system, args.source_dir))
    print("\n".join(report.lines()))
    print(f"Done in {time.monotonic() - started:.1f} s")


if __name__ == "__main__":
    main()
