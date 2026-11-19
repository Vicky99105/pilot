"""ICD-10-CM fixture slice for the Phase 1 tracer.

A hand-made subset that covers the tracer claim's conditions (acute appendicitis) and a few
common comorbidities. Codes and tabular titles follow ICD-10-CM. Only codes whose title we
are sure of are included: the generalized peritonitis codes under K35.2 are left out
because their fifth and sixth characters changed across recent releases.

The index rows are our own simplified main term and sub-term paths pointing at those codes,
not a copy of the CMS Alphabetic Index. The notes are short summaries in our own words.

The full CMS index and tabular load is Phase 2, pending download approval. Rows are marked
source="fixture" so that load can replace them.
"""

# (code, title, billable, notes). notes keys: includes, excludes1, excludes2, code_first,
# use_additional.
TABULAR: list[tuple[str, str, bool, dict[str, list[str]]]] = [
    # Appendicitis
    ("K35", "Acute appendicitis", False, {}),
    ("K35.2", "Acute appendicitis with generalized peritonitis", False, {}),
    ("K35.3", "Acute appendicitis with localized peritonitis", False, {}),
    (
        "K35.30",
        "Acute appendicitis with localized peritonitis, without perforation or gangrene",
        True,
        {},
    ),
    (
        "K35.31",
        "Acute appendicitis with localized peritonitis and gangrene, without perforation",
        True,
        {},
    ),
    (
        "K35.32",
        "Acute appendicitis with perforation and localized peritonitis, without abscess",
        True,
        {},
    ),
    (
        "K35.33",
        "Acute appendicitis with perforation and localized peritonitis, with abscess",
        True,
        {},
    ),
    ("K35.8", "Other and unspecified acute appendicitis", False, {}),
    ("K35.80", "Unspecified acute appendicitis", True, {}),
    ("K35.89", "Other acute appendicitis", False, {}),
    ("K35.890", "Other acute appendicitis without perforation or gangrene", True, {}),
    ("K35.891", "Other acute appendicitis without perforation, with gangrene", True, {}),
    ("K36", "Other appendicitis", True, {}),
    ("K37", "Unspecified appendicitis", True, {}),
    # Gallbladder
    ("K80", "Cholelithiasis", False, {}),
    ("K80.0", "Calculus of gallbladder with acute cholecystitis", False, {}),
    ("K80.00", "Calculus of gallbladder with acute cholecystitis without obstruction", True, {}),
    ("K80.10", "Calculus of gallbladder with chronic cholecystitis without obstruction", True, {}),
    ("K80.2", "Calculus of gallbladder without cholecystitis", False, {}),
    ("K80.20", "Calculus of gallbladder without cholecystitis without obstruction", True, {}),
    ("K81.0", "Acute cholecystitis", True, {}),
    ("K65.0", "Generalized (acute) peritonitis", True, {}),
    # Diabetes and other comorbidities
    (
        "E11",
        "Type 2 diabetes mellitus",
        False,
        {
            "use_additional": [
                "Use an additional code for current insulin use (Z79.4)",
                "Use an additional code for current oral hypoglycemic drug use (Z79.84)",
            ]
        },
    ),
    ("E11.65", "Type 2 diabetes mellitus with hyperglycemia", True, {}),
    ("E11.9", "Type 2 diabetes mellitus without complications", True, {}),
    ("E10.9", "Type 1 diabetes mellitus without complications", True, {}),
    (
        "I10",
        "Essential (primary) hypertension",
        True,
        {
            "includes": ["High blood pressure", "Hypertension (arterial) (essential) (primary)"],
            "excludes1": ["Hypertensive disease complicating pregnancy (O10-O11, O13-O16)"],
        },
    ),
    (
        "I25.10",
        "Atherosclerotic heart disease of native coronary artery without angina pectoris",
        True,
        {},
    ),
    ("E03.9", "Hypothyroidism, unspecified", True, {}),
    ("E78.5", "Hyperlipidemia, unspecified", True, {}),
    ("E66.9", "Obesity, unspecified", True, {}),
    ("J45.909", "Unspecified asthma, uncomplicated", True, {}),
    # Infections and symptoms
    ("J18", "Pneumonia, unspecified organism", False, {}),
    ("J18.0", "Bronchopneumonia, unspecified organism", True, {}),
    ("J18.1", "Lobar pneumonia, unspecified organism", True, {}),
    ("J18.9", "Pneumonia, unspecified organism", True, {}),
    ("A09", "Infectious gastroenteritis and colitis, unspecified", True, {}),
    ("A41.9", "Sepsis, unspecified organism", True, {}),
    ("N39.0", "Urinary tract infection, site not specified", True, {}),
    ("R10.31", "Right lower quadrant pain", True, {}),
    ("R10.9", "Unspecified abdominal pain", True, {}),
    ("R50.9", "Fever, unspecified", True, {}),
    # Status codes
    ("Z79.4", "Long term (current) use of insulin", True, {}),
    ("Z79.84", "Long term (current) use of oral hypoglycemic drugs", True, {}),
]

# (main term, path, code). The path starts with the main term; " > " separates sub-terms.
INDEX: list[tuple[str, str, str]] = [
    ("Appendicitis", "Appendicitis", "K37"),
    ("Appendicitis", "Appendicitis > acute", "K35.80"),
    ("Appendicitis", "Appendicitis > acute > with localized peritonitis", "K35.30"),
    (
        "Appendicitis",
        "Appendicitis > acute > with localized peritonitis > with gangrene",
        "K35.31",
    ),
    (
        "Appendicitis",
        "Appendicitis > acute > with perforation > with localized peritonitis",
        "K35.32",
    ),
    (
        "Appendicitis",
        "Appendicitis > acute > with perforation > with localized peritonitis > with abscess",
        "K35.33",
    ),
    ("Appendicitis", "Appendicitis > acute > with generalized peritonitis", "K35.2"),
    ("Appendicitis", "Appendicitis > acute > with gangrene", "K35.891"),
    ("Appendicitis", "Appendicitis > acute > other specified", "K35.890"),
    ("Appendicitis", "Appendicitis > chronic", "K36"),
    ("Appendicitis", "Appendicitis > recurrent", "K36"),
    ("Cholelithiasis", "Cholelithiasis", "K80.20"),
    ("Cholelithiasis", "Cholelithiasis > with cholecystitis > acute", "K80.00"),
    ("Cholelithiasis", "Cholelithiasis > with cholecystitis > chronic", "K80.10"),
    ("Calculus", "Calculus > gallbladder", "K80.20"),
    ("Cholecystitis", "Cholecystitis > acute", "K81.0"),
    ("Peritonitis", "Peritonitis > generalized", "K65.0"),
    ("Diabetes", "Diabetes > type 2", "E11.9"),
    ("Diabetes", "Diabetes > type 2 > with hyperglycemia", "E11.65"),
    ("Diabetes", "Diabetes > type 1", "E10.9"),
    ("Hypertension", "Hypertension", "I10"),
    ("Hypertension", "Hypertension > essential", "I10"),
    ("Disease", "Disease > coronary artery", "I25.10"),
    ("Hypothyroidism", "Hypothyroidism", "E03.9"),
    ("Hyperlipidemia", "Hyperlipidemia", "E78.5"),
    ("Obesity", "Obesity", "E66.9"),
    ("Asthma", "Asthma", "J45.909"),
    ("Pneumonia", "Pneumonia", "J18.9"),
    ("Pneumonia", "Pneumonia > lobar", "J18.1"),
    ("Bronchopneumonia", "Bronchopneumonia", "J18.0"),
    ("Gastroenteritis", "Gastroenteritis > infectious", "A09"),
    ("Colitis", "Colitis > infectious", "A09"),
    ("Sepsis", "Sepsis", "A41.9"),
    ("Infection", "Infection > urinary tract", "N39.0"),
    ("Pain", "Pain > abdominal", "R10.9"),
    ("Pain", "Pain > abdominal > right lower quadrant", "R10.31"),
    ("Fever", "Fever", "R50.9"),
    ("Long-term drug therapy", "Long-term drug therapy > insulin", "Z79.4"),
    ("Long-term drug therapy", "Long-term drug therapy > oral hypoglycemic", "Z79.84"),
]
