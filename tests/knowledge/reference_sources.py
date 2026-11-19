"""Small page texts and table rows shaped like what pypdfium2 and pdfplumber extract from the
public reference sources. Each is a short excerpt: enough to exercise the parsers on the real
layouts (running headers, wrapped cells, list numbering that restarts, headings in the middle
of a list), not a copy of the source.
"""

# --- Arogya Sanjeevani wording, Annexure A (Go Digit layout: item, then the product's note) --

WORDING_LISTS_P20 = """Go Digit General Insurance Limited
 Arogya Sanjeevani Policy, Go Digit - Policy Wording (GODHLIP24179V022324) Page 20 of 25
ANNEXURE A
List I \u2013 Optional Items
SI
No
Item
1. BABY FOOD (Not Payable)
2. BELTS/BRACES (Payable incases where insured has undergone Surgery of thoracic or lumbar
spine)
3. FOOD CHARGES (OTHER THAN PATIENT's DIET PROVIDED BY HOSPITAL) (Not Payable)
4. GLOVES (Sterilized Gloves Payable / Unsterilized Gloves not payable)
5. SERVICE CHARGES WHERE NURSING CHARGE ALSO CHARGED
6. NIMBUS BED OR WATER OR AIR BED CHARGES (Payable for any ICU patient requiring more
than 3 days in ICU, all patients with paraplegia
List II - Items that are to be subsumed into Room Charges
SI
No
Item
1 BABY CHARGES (UNLESS SPECIFIED/INDICATED) (Not Payable)
2 HOUSE KEEPING CHARGES (Part of Room Charge Not Payable Separately)
"""

WORDING_LISTS_P21 = """Go Digit General Insurance Limited
 Arogya Sanjeevani Policy, Go Digit - Policy Wording (GODHLIP24179V022324) Page 21 of 25
3 ADMISSION KIT (Not Payable)
List III - Items that are to be subsumed into Procedure Charges
SI
No.
Item
1 HAIR REMOVAL CREAM (Not Payable)
2 DISPOSABLES RAZORS CHARGES (for site preparations) (Payable for site preparations)
3 TORNIQUET Not payable (service is charged by hospital, consumables cannot be separately
charged.
List IV - Items that are to be subsumed into costs of treatment
SI
No.
Item
1 ADMISSION/REGISTRATION CHARGES (Not Payable)
2 HOSPITALISATION FOR EVALUATION/ DIAGNOSTIC PURPOSE Unless A Claim Is
Accepted Under Section1
3 URINE BAG (Payable where medically necessary till a reasonable cost - maximum 1 per 24 hrs)
List V \u2013 Additional Non Payable Items
Sr. No List of Expenses Generally Excluded ("Non-medical")
1. Brush
"""

# The IRDAI circular prints a list heading and a paragraph in the middle of List I.
CIRCULAR_LISTS_P26 = """Page 26 of 155
ANNEXURE I
Items for which optional cover may be offered by Insurers
1. LIST \u2013 I: Items that may be offered as optional items \u2013 Items specified in the list are
the Optional Items to which Insurers may offer coverage.
List I \u2013 Optional Items
Sl
No
Item
1 BABY FOOD
2 BELTS/ BRACES
3 FOOD CHARGES (OTHER THAN PATIENT's DIET PROVIDED BY HOSPITAL)
2. Where the costs are to be subsumed into the room charges specified in List \u2013 II or
procedure charges specified in List III or costs of treatment (including costs of
List II \u2013 Items that are to be subsumed into Room Charges
4 GLOVES
5 SERVICE CHARGES WHERE NURSING CHARGE ALSO CHARGED
6 NIMBUS BED OR WATER OR AIR BED CHARGES
Sl
No
Item
1 BABY CHARGES (UNLESS SPECIFIED/INDICATED)
2 HOUSE KEEPING CHARGES
3 ADMISSION KIT
"""

# --- IRDAI exclusions circular (CIR/177/09/2019), pp.6-9: OCR-like code spellings -----------

EXCLUSIONS_P6 = """CHAPTER Ill
A. Exclusion Name: Pre-Existing Diseases - Code- Excl01
a) Expenses related to the treatment of a pre-existing Disease (PED) and its direct
B. Exclusion Name: Specified disease/procedure waiting period- Code- Excl02
Page 6 of 17
"""

EXCLUSIONS_P8 = """L. Treatment for, Alcoholism, drug or substance abuse or any addictive
condition and
consequences thereof. Code- Exel 12
Page 8 of 17
"""

EXCLUSIONS_P9 = """M. Treatments received in heath hydros, nature cure clinics, spas or similar
Code- Excl13
0. Refractive Error: Code- Excl15
R. Maternity: Code Excl18
Page 9 of 17
"""

# The wording repeats the codes in its own numbering, then lists its specific exclusions.
WORDING_EXCLUSIONS_P10 = """15. Refractive Error:(Code- Exc115)
18. Maternity Expenses (Code - Excl18):
II. SPECIFIC EXCLUSIONS
"""

WORDING_EXCLUSIONS_P11 = """19. War (whether declared or not) and war like occurrence
or invasion, acts of foreign
20. Nuclear, chemical or biological attack or weapons, contributed to, caused by
21. Any expenses incurred on Domiciliary Hospitalization and OPD treatment
22. Treatment taken outside the geographical limits of India
23. In respect of the existing diseases, disclosed by the insured and mentioned in the policy
schedule, policyholder is not entitled to get the coverage for specified ICD codes.
"""

# --- Arogya Sanjeevani wording: benefits and the specified-condition lists ------------------

WORDING_BENEFITS_P6 = """C. BENEFITS COVERED UNDER THE POLICY
1. Hospitalization
i. Room Rent, Boarding, Nursing Expenses as provided by the Hospital / Nursing Home up to 2% of the
sum insured subject to maximum of Rs.5000 /-, per day.
ii. Intensive Care Unit (ICU)/ Intensive Cardiac Care Unit (ICCU) expenses up to 5% of sum
insured subject
to maximum of Rs. 10,000/- per day.
v. Expenses incurred on road Ambulance subject to a maximum of Rs.2000/- per hospitalisation.
3. Cataract Treatment
The Company shall indemnify medical expenses incurred for treatment of Cataract, subject to a
limit of
25% of Sum Insured or Rs.40,000/-, whichever is lower, per each eye in one policy year.
"""

WORDING_BENEFITS_P7 = """4. Pre-Hospitalization
The company shall indemnify pre-hospitalization medical expenses incurred, related to an admissible
hospitalization requiring inpatient care, for a fixed period of 30 days prior to the date of
admissible
5. Post Hospitalisation
The company shall indemnify post hospitalization medical expenses incurred, related to an admissible
hospitalization requiring inpatient care, for a fixed period of 60 days from the date of
discharge from the
6. The following procedures will be covered (wherever medically indicated) either as in patient
or as part of
day care treatment in a hospital up to 50% of Sum Insured, specified in the policy schedule,
during the
"""

WORDING_WAITING_P8 = """1. Pre-Existing Diseases (Code- Excl01)
a) Expenses related to the treatment of a pre-existing Disease (PED) and its direct
complications shall be
excluded until the expiry of 36months of continuous coverage after the date of inception of the
first policy
3. Specific Waiting Period: (Code- Excl02)
i. 24 Months waiting period
"""

WORDING_WAITING_P9 = """Go Digit General Insurance Limited
 Arogya Sanjeevani Policy, Go Digit - Policy Wording (GODHLIP24179V022324) Page 9 of 25
1. Benign ENT disorders
2. Hernia of all types
3. Calculi in urinary system, Gall Bladder and Bile duct, excluding
malignancy.
ii. 36Months waiting period
1. Treatment for joint replacement unless arising from accident
4. Investigation & Evaluation (Code- Excl04)
"""

WORDING_COPAY_P18 = """c. Co-payment
Each and every claim under the Policy shall be subject to a Co-payment of 5% applicable to
claim amount
"""

# --- CGHS OM 03.10.2025, Annexure I: rate rows (a wrapped name, a wrapped specialty) --------

CGHS_TIER1_P71 = """CGHS rates for Tier I (X City)
71
Sr.
No
CGHS
Code CGHS TREATMENT PROCEDURE/INVESTIGATION LIST
Rates for Semi-Private Ward
Speciality Classification Non￾NABH NABH Super
Speciality
15 LB012
Complete Haemogram/CBC, Hb, RBC Count and Indices, TLC, DLC, Platelet,
ESR, Peripheral Smear Examination) 255 300 300 Laboratory Investigation
1393 AG045 Appendicectomy 17765 20900 24035
Abdomen/GI Surgery
Procedure
1997 HC001
Annual Health Check-up - CCS Group A Officer of above 40 years of age /
Pensioner primary card holder 75 years of age and above\u2013Male, 2000 2000 2000 Annual
Health Check-up
5-16/CGHS(HQ)/HEC/2024(PartI) I/3807087/2025
"""

CGHS_TIER2_P160 = """CGHS rates for Tier II (Y City)
1393 AG045 Appendicectomy 15989 18810 21632
Abdomen/GI Surgery
Procedure
5-16/CGHS(HQ)/HEC/2024(PartI) I/3807087/2025
"""

CGHS_RULES_P1 = """2. Structure of Differential Rates
a) Non-NABH and Non-NABL HCOs: 15% lower than NABH/NABL accredited
b) Rates for super speciality hospitals shall be 15% higher than those
c) HCO located in Y (Tier II) cities and Z (Tier III) cities rates shall be 10% and
20% respectively lower than those located in X (Tier I) Cities.
"""

CGHS_CITIES_P4 = """List of X (Tier I) Cities
Hyderabad (UA)# Delhi (UA) Ahmedabad (UA) Bengaluru (UA)
Mumbai (UA) Pune (UA) Chennai (UA) Kolkata (UA)
"""

CGHS_CITIES_P109 = """*Indicative List of Y(Tier II) Cities
Vijayawad
a
Warangal Gurgaon Lucknow Noida Faridabad Ghaziaba
d
Jalandhar Chandigar
h
"""

# --- PM-JAY HBP 2022 (Nagaland SHA), pdfplumber table rows ----------------------------------

HBP_HEADER = [
    "Specialty", "Specialty Code HBP 2022", "AB PMJAY Package Name", "Procedure code HBP 2022",
    "Procedure Name 2022", "Tier3 (Z)", "Implant mapped.", "implant cost",
    "Stratificatio n Criteria (Y/N)", "Stratification remarks", "Multiple Procedures",
    "Reservation Public Hospitals (Y/N)", "LOS",
]  # fmt: skip
HBP_TABLE_P210: list[list[str | None]] = [
    ["The rates are for general ward. All beneficiaries", "", "", "", "", "", "", "", "", "", "",
     "", ""],
    HBP_HEADER,
    ["General\nSurgery,\nPediatric\nSurgery", "SG", "Appendicectomy", "SG017A",
     "Appendicectomy- Open", "21700", "NA", "NA", "No", "NA", "Yes", "No", "3"],
    ["Plastic &\nReconstructiv\ne", "SP", "Revascularization of limb", "SP03B",
     "Revascularization of\nlimb", "-\n112500", "B. >1digit/lim\nb : cost -", "90000", "",
     "NA", "No", "No", "5"],
    ["Urology", "SU", "Suprapubic Drainage", "- SU061A", "Suprapubic Drainage - Closed\n"
     "(Cystofix)", "6 400", "NA", "NA", "N o", "NA", "No", "NO", "3-5 days"],
    ["General medicine", "MG", "Pleural Effusion", "MG0111A", "Pleural Effusion", "0", "NA",
     "NA", "Yes", "Routine Ward - 2310HDU - 3300", "No", "No", "3-5 days"],
    ["Nagaland", "NAG", "State package", "NAG001", "State-only procedure", "5000", "NA", "NA",
     "No", "NA", "No", "No", "2"],
]  # fmt: skip

# --- Kerala MEDISEP Phase II, Annexure IA: pdfplumber table rows ----------------------------

MEDISEP_TABLE_P200: list[list[str | None]] = [
    ["MEDISEP\nPackage\nCode", "Specialty", "Package\nName", "Procedure Name", "Length\nof Stay",
     "Implant\nMapped", "Implant\nCost\n(maximum\npayable)", "No NABH", "ENTRY Level\nNABH",
     "NABH Fully\nAccredited &\nGovt Medical\nColleges", "Add on\nApplicable", "Procedure\nLabel",
     "Medical or\nSurgical (S/M)"],
    ["MS1421", "General Surgery", "Pancreatic\nNecrosectom\ny", "Pancreatic Necrosectomy", "6",
     "NA", "NA", "86,250", "93,750", "112,500", "NO", "Regular Procedure", "Surgical"],
    ["MS0310", "General Medicine", "Acute\ngastroenteritis", "Acute gastroenteritis with\n"
     "moderate dehydration", "Day care", "NA", "NA", "1,800", "2,000", "2,300", "Yes",
     "Add - On Procedure", "Medical"],
]  # fmt: skip

# --- NPPA S.O. 1575(E), 25.03.2026: English table rows (pdfplumber) -------------------------

NPPA_TABLE_P31: list[list[str | None]] = [
    ["Sl. No.", "Medicines", "Dosage form and strength", "Unit",
     "Ceiling price (w.e.f. 1.4.2026 with WPI @ 0.64956%) (in Rs.)",
     "Existing S.O., No. & Date", ""],
    ["(1)", "(2)", "(3)", "(4)", "(5)", "(6a)", "(6b)"],
    ["1", "5-amino salicylic Acid\n(Mesalazine/Mesalamine)", "Suppository 500 mg",
     "1 Suppository", "17.86", "2027(E)", "06-05-2025"],
    ["573", "Paracetamol", "Tablet 500mg", "1 Tablet", "0.93", "1489(E)", "27-03-2025"],
    ["576", "Paracetamol", "Injection 150mg/mL\n(2ML)", "1 ML", "4.07", "1489(E)", "27-03-2025"],
]  # fmt: skip

# --- NLEM 2022 (NHSRC report): medicines with levels of care and formulations ---------------

NLEM_P8 = """Medicine Level of
Healthcare Dosage form(s) and strength(s)
2.1.2 Diclofenac P,S,T Tablet 50 mg
Injection 25 mg/mL
2.1.5 Paracetamol** P,S,T
Tablet 500 mg
Oral liquid 120 mg/5 mL (p)
2.2-Opioid Analgesics
Medicine Level of
Healthcare Dosage form(s) and strength(s)
2.2.1 Fentanyl S,T Injection 50 mcg/mL
** Paracetamol formulations are also listed in -
A. Section 5.2.3 - Medicines used in Neurological Disorders-Antimigraine Medicines
"""

NLEM_P7 = """Section 2
Analgesics, Antipyretics, Non-steroidal Anti-inflammatory Drugs (NSAIDs)
2.1.1 Acetylsalicylic
acid** P,S,T
Tablet 300 mg to 500 mg
Effervescent/ Dispersible/ Enteric
coated Tablet 300 mg to 500 mg
"""

NLEM_P83 = """Section 27
Medicines for COVID 19 management
27.4 Paracetamol** P,S,T
Tablet 500 mg
"""

# --- PM-JAY STGs: the compilation table (pdfplumber) and one STG PDF's text ------------------

STG_HEADER = ["Sr. No.", "STG Set", "Speciality", "Package Name", "Procedure Name",
              "Procedure code", "Amount", "LOS", "Treating doctor\nqualification",
              "Documents required\nat Preauth", "Documents\nrequired at Claims"]  # fmt: skip
STG_TABLE_P120: list[list[str | None]] = [
    STG_HEADER,
    ["801", "Set 3", "General Surgery", "Appendicectomy", "Appendicectomy Open", "SG017A",
     "11,000", "3 days", "MS / DNB", "a. Clinical notes", "a. Indoor case papers"],
    ["802", "Set 3", "General Surgery", "Appendicectomy", "Appendicectomy Open", "SG017A",
     "11,000", "3 days", "MS / DNB", "b. USG Abdomen", "b. Histopathology\nreport"],
    ["803", "Set 3", "General Surgery", "Appendicectomy", "Appendicectomy Open", "SG017A",
     "11,000", "3 days", "MS / DNB", "", "c. Discharge summary"],
    # A garbled row: the package name overflowed into the next cells.
    ["1546", "Set 8", "General Surgery", "Congenital Atresia", "C&o nSgteennoitsaisl A",
     "nStGes0t1in4eA", "23,000", "1-4 weeks", "MCh", "a. Clinical notes", "a. ICPs"],
]  # fmt: skip
STG_TEXT_P219 = """1546 Set 8 General Surgery Congenital Atresia & Stenosis of Small
Intestine SG014A 23,000
1-4 weeks Essential:
"""

STG_PDF_P1 = """National Health Authority Version 1.1 Dated June 2020
Guidance document for processing PM-JAY packages
Appendicectomy/Appendicular Perforation/Appendicular Abscess
Package name Procedure name HBP 1.0
code
HBP 2.0 code Package
price
ALOS
Appendicectomy Open S100003 SG017A 11,000 3 days
Appendicectomy Lap S100003 SG017B 11,000 3 days
1.2 Clinical key pointers:
a. Pain starting in the central abdomen and then shifting to the lower right side of abdomen
b. Nausea or Vomiting
1.3 Mandatory documents- For healthcare providers
Clinical notes Yes Yes Yes
"""

STG_PDF_P2 = """National Health Authority Version 1.1 Dated June 2020
2.2.1 At the time of pre-authorization processing- For pre-authorisation processing doctor (PPD)
a. Clinical notes clearly indicating pain in abdomen and the need for surgery? Yes
2.2.2 At the time of claim processing- For claims processing doctor (CPD)
a. Is there documentary evidence of indication of surgery? Yes
b. Are detailed operative notes available with indications for surgery and outcomes of the
procedure? Yes
PART III: GUIDELINES FOR IT
3.2 Below mentioned are the scenarios where a provision would be built in TMS for pop-ups:
a. Appendectomy has never been done in the past? \u2013 Yes
Till the time the functionality is being developed, the processing doctor shall check the above
manually.
References:
1. Clinical pathways, General Surgery
"""
