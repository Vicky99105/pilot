"""Small ICD source files for the loader tests, built to the CMS XSDs (icd10cm_tabular.xsd,
icd10cm_index.xsd, icd10cm_drug_neoplasm.xsd, icd10pcs_tables.xsd, icd10pcs_definitions.xsd)
and the fixed-width order file layout. They are packed into zips with the same archive and
member names as the CMS FY2027 release, so the loaders read them exactly as they read the
real files. The content is a hand-made subset; titles follow the FY2027 release."""

import zipfile
from pathlib import Path

CM_TABULAR = """<?xml version="1.0" encoding="utf-8"?>
<ICD10CM.tabular>
  <version>2027</version>
  <introduction>
    <introSection type="title">
      <title>ICD-10-CM TABULAR LIST of DISEASES and INJURIES</title>
    </introSection>
    <introSection type="instruction">
      <title>Includes:</title>
      <instruction><note>Introduction notes are not loaded.</note></instruction>
    </introSection>
  </introduction>
  <chapter>
    <name>4</name>
    <desc>Endocrine, nutritional and metabolic diseases (E00-E89)</desc>
    <section id="E08-E13">
      <desc>Diabetes mellitus (E08-E13)</desc>
      <diag>
        <name>E11</name>
        <desc>Type 2 diabetes mellitus</desc>
        <useAdditionalCode>
          <note>code to identify control using insulin (Z79.4)</note>
        </useAdditionalCode>
        <excludes1>
          <note>diabetes mellitus due to underlying condition (E08.-)</note>
        </excludes1>
        <diag>
          <name>E11.9</name>
          <desc>Type 2 diabetes mellitus without complications</desc>
        </diag>
      </diag>
    </section>
  </chapter>
  <chapter>
    <name>11</name>
    <desc>Diseases of the digestive system (K00-K95)</desc>
    <excludes2>
      <note>certain conditions originating in the perinatal period (P04-P96)</note>
    </excludes2>
    <section id="K35-K38">
      <desc>Diseases of appendix (K35-K38)</desc>
      <diag>
        <name>K35</name>
        <desc>Acute appendicitis</desc>
        <codeAlso>
          <note>if applicable, any associated <new>sepsis</new> (A41.-)</note>
        </codeAlso>
        <diag>
          <name>K35.3</name>
          <desc>Acute appendicitis with localized peritonitis</desc>
          <diag>
            <name>K35.30</name>
            <desc>Acute appendicitis with localized peritonitis,
              without perforation or gangrene</desc>
            <inclusionTerm>
              <note>Acute appendicitis with localized peritonitis NOS</note>
            </inclusionTerm>
          </diag>
          <diag>
            <name>K35.32</name>
            <desc>Acute appendicitis with perforation, localized peritonitis, and gangrene,
              without abscess</desc>
            <inclusionTerm>
              <note>(Acute) appendicitis with perforation NOS</note>
              <note>Perforated appendix NOS</note>
            </inclusionTerm>
          </diag>
        </diag>
        <diag>
          <name>K35.8</name>
          <desc>Other and unspecified acute appendicitis</desc>
          <diag>
            <name>K35.80</name>
            <desc>Unspecified acute appendicitis</desc>
            <inclusionTerm>
              <note>Acute appendicitis NOS</note>
            </inclusionTerm>
          </diag>
          <diag>
            <name>K35.89</name>
            <desc>Other acute appendicitis</desc>
            <diag>
              <name>K35.890</name>
              <desc>Other acute appendicitis without perforation or gangrene</desc>
            </diag>
            <diag>
              <name>K35.891</name>
              <desc>Other acute appendicitis without perforation, with gangrene</desc>
            </diag>
          </diag>
        </diag>
      </diag>
      <diag>
        <name>K37</name>
        <desc>Unspecified appendicitis</desc>
        <excludes1>
          <note>unspecified appendicitis with peritonitis (K35.2-K35.3)</note>
        </excludes1>
      </diag>
    </section>
  </chapter>
  <chapter>
    <name>19</name>
    <desc>Injury, poisoning and certain other consequences of external causes (S00-T88)</desc>
    <section id="S70-S79">
      <desc>Injuries to the hip and thigh (S70-S79)</desc>
      <diag>
        <name>S72</name>
        <desc>Fracture of femur</desc>
        <codeFirst>
          <note>any associated open wound</note>
        </codeFirst>
        <notes>
          <note>A fracture not indicated as displaced or nondisplaced should be coded to
            displaced</note>
        </notes>
        <sevenChrNote>
          <note>The appropriate 7th character is to be added to all codes from category S72</note>
        </sevenChrNote>
        <sevenChrDef>
          <extension char="A">initial encounter for closed fracture</extension>
          <extension char="D">subsequent encounter for closed fracture
            with routine healing</extension>
        </sevenChrDef>
        <diag>
          <name>S72.0</name>
          <desc>Fracture of head and neck of femur</desc>
          <diag>
            <name>S72.00</name>
            <desc>Fracture of unspecified part of neck of femur</desc>
            <diag>
              <name>S72.001</name>
              <desc>Fracture of unspecified part of neck of right femur</desc>
            </diag>
          </diag>
        </diag>
      </diag>
    </section>
  </chapter>
</ICD10CM.tabular>
"""

# (code without the dot, valid for submission, long title). The short title is cut from the
# long one, as the CMS file abbreviates it.
CM_ORDER = [
    ("E11", False, "Type 2 diabetes mellitus"),
    ("E119", True, "Type 2 diabetes mellitus without complications"),
    ("K35", False, "Acute appendicitis"),
    ("K353", False, "Acute appendicitis with localized peritonitis"),
    (
        "K3530",
        True,
        "Acute appendicitis with localized peritonitis, without perforation or gangrene",
    ),
    (
        "K3532",
        True,
        "Acute appendicitis with perforation, localized peritonitis, and gangrene, without abscess",
    ),
    ("K358", False, "Other and unspecified acute appendicitis"),
    ("K3580", True, "Unspecified acute appendicitis"),
    ("K3589", False, "Other acute appendicitis"),
    ("K35890", True, "Other acute appendicitis without perforation or gangrene"),
    ("K35891", True, "Other acute appendicitis without perforation, with gangrene"),
    ("K37", True, "Unspecified appendicitis"),
    ("S72", False, "Fracture of femur"),
    ("S720", False, "Fracture of head and neck of femur"),
    ("S7200", False, "Fracture of unspecified part of neck of femur"),
    ("S72001", False, "Fracture of unspecified part of neck of right femur"),
    (
        "S72001A",
        True,
        "Fracture of unspecified part of neck of right femur, initial encounter for closed "
        "fracture",
    ),
    (
        "S72001D",
        True,
        "Fracture of unspecified part of neck of right femur, subsequent encounter for closed "
        "fracture with routine healing",
    ),
]

CM_INDEX = """<?xml version="1.0" encoding="utf-8"?>
<ICD10CM.index>
  <version>2027</version>
  <title>ICD-10-CM INDEX TO DISEASES and INJURIES</title>
  <letter>
    <title>A</title>
    <mainTerm>
      <title>Abandonment</title>
      <see>Maltreatment</see>
    </mainTerm>
    <mainTerm>
      <title>Appendicitis<nemod>(pneumococcal) (retrocecal)</nemod></title>
      <code>K37</code>
      <term level="1">
        <title>with</title>
        <term level="2">
          <title>perforation NOS</title>
          <code>K35.32</code>
        </term>
      </term>
      <term level="1">
        <title>acute<nemod>(catarrhal) (fulminating)</nemod></title>
        <code>K35.80</code>
        <term level="2">
          <title>specified NEC</title>
          <code>K35.89-</code>
        </term>
      </term>
      <term level="1">
        <title>chronic</title>
        <seeAlso>Appendicitis, recurrent</seeAlso>
      </term>
    </mainTerm>
  </letter>
  <letter>
    <title>D</title>
    <mainTerm>
      <title>Diabetes, diabetic<nemod>(mellitus)</nemod></title>
      <code>E11.9</code>
      <term level="1">
        <title>with</title>
        <term level="2">
          <title>amyotrophy</title>
          <code>E11.44</code>
          <manif>G73.3</manif>
        </term>
      </term>
    </mainTerm>
  </letter>
  <letter>
    <title>F</title>
    <mainTerm>
      <title>Fracture, traumatic<nemod>(abduction) (adduction)</nemod></title>
      <term level="1">
        <title>femur, femoral</title>
        <term level="2">
          <title>neck</title>
          <code>S72.00-</code>
        </term>
      </term>
    </mainTerm>
  </letter>
</ICD10CM.index>
"""

CM_EINDEX = """<?xml version="1.0" encoding="utf-8"?>
<ICD10CM.index>
  <version>2027</version>
  <title>ICD-10-CM EXTERNAL CAUSE of INJURIES INDEX</title>
  <letter>
    <title>A</title>
    <mainTerm>
      <title>Accident<nemod>(to)</nemod> NEC</title>
      <code>X58</code>
      <term level="1">
        <title>automobile</title>
        <see>Accident, transport, car occupant</see>
      </term>
    </mainTerm>
  </letter>
</ICD10CM.index>
"""

_HEADINGS = {
    "drug": [
        "Substance",
        "Poisoning Accidental (unintentional)",
        "Poisoning Intentional self-harm",
        "Poisoning Assault",
        "Poisoning Undetermined",
        "Adverse effect",
        "Underdosing",
    ],
    "neoplasm": [
        "Neoplasm",
        "Malignant Primary",
        "Malignant Secondary",
        "Ca in situ",
        "Benign",
        "Uncertain Behavior",
        "Unspecified Behavior",
    ],
}


def _heading(kind: str) -> str:
    heads = "".join(f'<head col="{i}">{h}</head>' for i, h in enumerate(_HEADINGS[kind], 1))
    return f"<indexHeading>{heads}</indexHeading>"


def _cells(*codes: str) -> str:
    return "".join(f'<cell col="{i}">{c}</cell>' for i, c in enumerate(codes, 2))


CM_DRUG = f"""<?xml version="1.0" encoding="utf-8"?>
<ICD10CM.index>
  <version>2027</version>
  {_heading("drug")}
  <letter>
    <title>P</title>
    <mainTerm>
      <title>Paracetamol</title>
      {_cells("T39.1X1", "T39.1X2", "T39.1X3", "T39.1X4", "T39.1X5", "T39.1X6")}
    </mainTerm>
    <mainTerm>
      <title>Petrol<nemod>(vapor)</nemod></title>
      {_cells("T52.0X1", "T52.0X2", "T52.0X3", "T52.0X4", "--", "--")}
    </mainTerm>
  </letter>
</ICD10CM.index>
"""

CM_NEOPLASM = f"""<?xml version="1.0" encoding="utf-8"?>
<ICD10CM.index>
  <version>2027</version>
  {_heading("neoplasm")}
  <letter>
    <title>N</title>
    <mainTerm>
      <title>Neoplasm, neoplastic</title>
      {_cells("C80.1", "C79.9", "D09.9", "D36.9", "D48.9", "D49.9")}
      <term level="1">
        <title>appendix</title>
        <seeAlso>Neoplasm, intestine</seeAlso>
        {_cells("C18.1", "C78.5", "D01.0", "D12.1", "D37.3", "-")}
      </term>
    </mainTerm>
  </letter>
</ICD10CM.index>
"""


def _axis(pos: int, title: str, labels: list[tuple[str, str]], definition: str = "") -> str:
    body = "".join(f'<label code="{c}">{text}</label>' for c, text in labels)
    extra = f"<definition>{definition}</definition>" if definition else ""
    return f'<axis pos="{pos}" values="{len(labels)}"><title>{title}</title>{body}{extra}</axis>'


def _row(
    body: list[tuple[str, str]],
    approach: list[tuple[str, str]],
    device: list[tuple[str, str]],
    qualifier: list[tuple[str, str]],
    titles: tuple[str, str, str, str] = ("Body Part", "Approach", "Device", "Qualifier"),
) -> str:
    codes = len(body) * len(approach) * len(device) * len(qualifier)
    axes = "".join(
        _axis(pos, title, labels)
        for pos, title, labels in zip(
            (4, 5, 6, 7), titles, (body, approach, device, qualifier), strict=True
        )
    )
    return f'<pcsRow codes="{codes}">{axes}</pcsRow>'


_SURGERY = [("0", "Medical and Surgical")]
_GI = [("D", "Gastrointestinal System")]
APPENDIX = ("J", "Appendix")
OPEN = ("0", "Open")
PERC_ENDO = ("4", "Percutaneous Endoscopic")
NO_DEVICE = ("Z", "No Device")
NO_QUALIFIER = ("Z", "No Qualifier")
EXCISION = "Cutting out or off, without replacement, a portion of a body part"
RESECTION = "Cutting out or off, without replacement, all of a body part"

_IMAGING_ROW = _row(
    [("0", "Coronary Artery, Single")],
    [("0", "High Osmolar")],
    [("Z", "None")],
    [("Z", "None")],
    ("Body Part", "Contrast", "Qualifier", "Qualifier"),
)

PCS_TABLES = f"""<?xml version="1.0" encoding="utf-8"?>
<ICD10PCS.tabular>
  <version>2027</version>
  <title>2027 ICD-10-PCS Tables</title>
  <pcsTable>
    {_axis(1, "Section", _SURGERY)}
    {_axis(2, "Body System", _GI)}
    {_axis(3, "Operation", [("B", "Excision")], EXCISION)}
    {_row([APPENDIX], [OPEN, PERC_ENDO], [NO_DEVICE], [("X", "Diagnostic"), NO_QUALIFIER])}
  </pcsTable>
  <pcsTable>
    {_axis(1, "Section", _SURGERY)}
    {_axis(2, "Body System", _GI)}
    {_axis(3, "Operation", [("T", "Resection")], RESECTION)}
    {_row([APPENDIX, ("K", "Ascending Colon")], [OPEN, PERC_ENDO], [NO_DEVICE], [NO_QUALIFIER])}
    {_row([APPENDIX], [PERC_ENDO], [NO_DEVICE], [("G", "Hand-Assisted")])}
  </pcsTable>
  <pcsTable>
    {_axis(1, "Section", [("B", "Imaging")])}
    {_axis(2, "Body System", [("2", "Heart")])}
    {_axis(3, "Type", [("0", "Plain Radiography")])}
    {_IMAGING_ROW}
  </pcsTable>
</ICD10PCS.tabular>
"""

PCS_DEFINITIONS = """<?xml version="1.0" encoding="utf-8"?>
<ICD10PCS.definitions>
  <version>2027</version>
  <title>2027 ICD-10-PCS Definitions</title>
  <section code="0">
    <title>Medical and Surgical</title>
    <axis pos="3">
      <title>Operation</title>
      <terms>
        <title>Excision</title>
        <definition>Cutting out or off, without replacement, a portion of a body part</definition>
        <includes>Partial nephrectomy, liver biopsy</includes>
      </terms>
      <terms>
        <title>Resection</title>
        <definition>Cutting out or off, without replacement, all of a body part</definition>
        <includes>Total nephrectomy, total lobectomy of lung</includes>
      </terms>
    </axis>
    <axis pos="4">
      <title>Body Part</title>
      <terms>
        <title>Appendix</title>
        <includes>Vermiform appendix</includes>
      </terms>
    </axis>
    <axis pos="5">
      <title>Approach</title>
      <terms>
        <title>Open</title>
        <definition>Cutting through the skin or mucous membrane and any other body layers
          necessary to expose the site of the procedure</definition>
      </terms>
      <terms>
        <title>Percutaneous Endoscopic</title>
        <definition>Entry, by puncture or minor incision, of instrumentation through the
          skin or mucous membrane and any other body layers necessary to reach and visualize
          the site of the procedure</definition>
      </terms>
    </axis>
  </section>
  <deviceAggregation>
    <aggregate>
      <device>Cardiac Lead, Pacemaker</device>
      <operation>Insertion</operation>
      <bodySys>2</bodySys>
      <parent value="M">Cardiac Lead</parent>
    </aggregate>
  </deviceAggregation>
</ICD10PCS.definitions>
"""

# Every valid code the tables above make, with the table headers the order file also lists.
PCS_ORDER = [
    ("0DB", False, "Excision of Gastrointestinal System"),
    ("0DBJ0ZX", True, "Excision of Appendix, Open Approach, Diagnostic"),
    ("0DBJ0ZZ", True, "Excision of Appendix, Open Approach"),
    ("0DBJ4ZX", True, "Excision of Appendix, Percutaneous Endoscopic Approach, Diagnostic"),
    ("0DBJ4ZZ", True, "Excision of Appendix, Percutaneous Endoscopic Approach"),
    ("0DT", False, "Resection of Gastrointestinal System"),
    ("0DTJ0ZZ", True, "Resection of Appendix, Open Approach"),
    ("0DTJ4ZG", True, "Resection of Appendix, Percutaneous Endoscopic Approach, Hand-Assisted"),
    ("0DTJ4ZZ", True, "Resection of Appendix, Percutaneous Endoscopic Approach"),
    ("0DTK0ZZ", True, "Resection of Ascending Colon, Open Approach"),
    ("0DTK4ZZ", True, "Resection of Ascending Colon, Percutaneous Endoscopic Approach"),
    ("B20", False, "Plain Radiography of Heart"),
    ("B2000ZZ", True, "Plain Radiography of Single Coronary Artery using High Osmolar Contrast"),
]


def order_file(lines: list[tuple[str, bool, str]]) -> str:
    """The CMS order file layout: order number (5), code (7), valid flag (1), short title
    (60), long title, separated by single spaces."""
    return "".join(
        f"{n:05d} {code:<7} {int(valid)} {title[:60]:<60} {title}\n"
        for n, (code, valid, title) in enumerate(lines, 1)
    )


CM_TABLES_ZIP = "2027-code-tables-tabular-index.zip"
CM_ORDER_ZIP = "2027-code-descriptions-tabular-order.zip"
PCS_TABLES_ZIP = "2027-icd-10-pcs-code-tables-index.zip"
PCS_ORDER_ZIP = "2027-icd-10-pcs-order-file-long-abbreviated-titles.zip"


def write_sources(directory: Path) -> Path:
    """Write the four CMS archives with the fixture members into directory."""
    directory.mkdir(parents=True, exist_ok=True)
    archives = {
        CM_TABLES_ZIP: {
            "Table and Index/icd10cm_tabular_2027.xml": CM_TABULAR,
            "Table and Index/icd10cm_index_2027.xml": CM_INDEX,
            "Table and Index/icd10cm_eindex_2027.xml": CM_EINDEX,
            "Table and Index/icd10cm_drug_2027.xml": CM_DRUG,
            "Table and Index/icd10cm_neoplasm_2027.xml": CM_NEOPLASM,
        },
        CM_ORDER_ZIP: {"Code Descriptions/icd10cm_order_2027.txt": order_file(CM_ORDER)},
        PCS_TABLES_ZIP: {
            "icd10pcs_tables_2027.xml": PCS_TABLES,
            "icd10pcs_definitions_2027.xml": PCS_DEFINITIONS,
        },
        PCS_ORDER_ZIP: {"icd10pcs_order_2027.txt": order_file(PCS_ORDER)},
    }
    for name, members in archives.items():
        with zipfile.ZipFile(directory / name, "w") as archive:
            for member, text in members.items():
                archive.writestr(member, text)
    return directory
