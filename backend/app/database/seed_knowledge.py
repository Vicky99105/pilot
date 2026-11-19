"""Knowledge rows for the Phase 1 tracer: policy clauses and non-payable items.

The clause texts are short summaries in our own words of an Arogya Sanjeevani style
standard product. They are not the policy wording. The non-payable items are a fixture
subset of the IRDAI lists I to IV, chosen for the tracer claim, with item names in our own
words. Item numbers are our own order within each list, not the IRDAI numbering.
"""

# (clause_ref, title, text). Every clause cited in config/products/ must appear here.
POLICY_CLAUSES: list[tuple[str, str, str]] = [
    (
        "64VB",
        "Premium received before cover",
        "Cover begins only once the insurer has the premium in hand. If the premium was not "
        "received on or before the cover start date, the policy gives no cover for that "
        "period (Section 64VB of the Insurance Act, 1938).",
    ),
    (
        "POLICY-PERIOD",
        "Policy period",
        "A claim is payable when the hospitalisation starts on or after the policy start "
        "date and within the period the premium paid for.",
    ),
    (
        "GRACE",
        "Grace period",
        "A late renewal or instalment premium may be paid within a grace period of 15 days "
        "for monthly payment and 30 days for every other payment mode. Cover continues "
        "during the grace period when the premium is paid within it.",
    ),
    (
        "INSURED",
        "Insured persons",
        "Only a person named in the policy schedule is covered. The patient on a claim must "
        "be identified as one of the insured persons.",
    ),
    (
        "SUM-INSURED",
        "Sum insured and cumulative bonus",
        "The most the insurer pays in a policy year is the sum insured plus any cumulative "
        "bonus earned, less amounts already paid or held for open claims.",
    ),
    (
        "ROOM-CAP",
        "Room, boarding and nursing limit",
        "Room, boarding and nursing charges are paid up to 2% of the sum insured for each "
        "day, and never more than Rs 5,000 a day.",
    ),
    (
        "ICU-CAP",
        "Intensive care limit",
        "Intensive care (ICU or ICCU) charges are paid up to 5% of the sum insured for each "
        "day, and never more than Rs 10,000 a day.",
    ),
    (
        "PROP-DED",
        "Proportionate deduction",
        "If the room occupied costs more a day than the room limit, charges that depend on "
        "the room category are paid in the same proportion as the room limit bears to the "
        "room rate charged. Pharmacy, consumables, implants and diagnostics are not reduced. "
        "No reduction applies when the hospital charges the same rates for every room "
        "category.",
    ),
    (
        "CO-PAY",
        "Co-payment",
        "The insured person bears 5% of every admissible claim. The co-payment is applied "
        "after all other limits and deductions.",
    ),
    (
        "FRAUD",
        "Fraudulent claims",
        "Nothing is paid on a claim that is fraudulent, or that is backed by a false "
        "statement or a false document. Any amount already paid on such a claim must be "
        "repaid to the insurer.",
    ),
    (
        "Excl01",
        "Pre-existing diseases",
        "Conditions that existed before the first policy are excluded until 36 months of "
        "continuous cover have passed, unless the policy says otherwise.",
    ),
    (
        "Excl02",
        "Specified diseases and procedures",
        "The illnesses and procedures named in the policy list are excluded until 24 months "
        "of continuous cover have passed.",
    ),
    (
        "Excl03",
        "First 30 days",
        "Treatment of an illness that begins within 30 days of the first policy start is "
        "excluded. The wait does not apply to hospitalisation caused by an accident, or "
        "when the insured person has had more than 12 months of continuous cover.",
    ),
    (
        "ANNEX-I",
        "Non-payable items, list I",
        "Items on list I of the annexure are not paid under the policy.",
    ),
    (
        "ANNEX-II",
        "Items within room charges, list II",
        "Items on list II are part of the room charges and are not paid as separate items.",
    ),
    (
        "ANNEX-III",
        "Items within procedure charges, list III",
        "Items on list III are part of the procedure or surgery charges and are not paid as "
        "separate items.",
    ),
    (
        "ANNEX-IV",
        "Items within treatment costs, list IV",
        "Items on list IV are part of the cost of treatment and are not paid as separate items.",
    ),
    (
        "CATARACT",
        "Cataract treatment",
        "Cataract surgery is paid up to 25% of the sum insured or Rs 40,000 for each eye, "
        "whichever is lower, in a policy year.",
    ),
    (
        "MODERN-TX",
        "Modern treatments",
        "The listed modern treatments, such as robotic surgery, stereotactic radiosurgery "
        "and deep brain stimulation, are paid up to 50% of the sum insured.",
    ),
    (
        "PRE-POST",
        "Pre and post hospitalisation",
        "Medical expenses in the 30 days before admission and the 60 days after discharge "
        "are paid when they relate to the same condition. Expenses outside those windows "
        "are not paid.",
    ),
    (
        "AMBULANCE",
        "Ambulance",
        "Road ambulance charges are paid up to Rs 2,000 for each hospitalisation.",
    ),
    (
        "AGREED-TARIFF",
        "Agreed package rates",
        "At a network hospital, a treatment the hospital's agreement prices as a package is "
        "paid at the agreed package rate. Several procedures in one admission are paid at "
        "100% of the highest-rated package, 50% of the second and 25% of each one after.",
    ),
    (
        "PACKAGE-INCL",
        "Items included in a package",
        "Services a package covers, such as the room, nursing, operation theatre, fees, "
        "medicines, disposables and routine investigations, are not paid again when the "
        "hospital bills them beside the package.",
    ),
    (
        "NON-PKG-DISC",
        "Non-package discount",
        "Charges outside a package are paid at the hospital's tariff less the discount in its "
        "agreement. Medicines and implants are not discounted.",
    ),
    (
        "PREAUTH",
        "Pre-authorisation limit",
        "A cashless claim is paid up to the amount approved at pre-authorisation and any "
        "approved enhancement. A change of diagnosis needs a fresh approval.",
    ),
    (
        "Excl07",
        "Change-of-gender treatments",
        "Treatment to change the characteristics of the body to those of the opposite sex is "
        "excluded.",
    ),
    (
        "Excl08",
        "Cosmetic or plastic surgery",
        "Cosmetic or plastic surgery is excluded unless it reconstructs after an accident, "
        "burns or cancer, or is medically necessary.",
    ),
    (
        "Excl12",
        "Alcoholism and substance abuse",
        "Treatment for alcoholism, drug or substance abuse, or any addictive condition, is "
        "excluded.",
    ),
    (
        "Excl17",
        "Sterility and infertility",
        "Treatment for sterility and infertility, including assisted reproduction, is excluded.",
    ),
    (
        "Excl18",
        "Maternity",
        "Childbirth and other maternity expenses are excluded, except ectopic pregnancy.",
    ),
]

_CONDITIONS = {
    "I": "Not paid under the policy",
    "II": "Part of the room charges; not paid separately",
    "III": "Part of the procedure or surgery charges; not paid separately",
    "IV": "Part of the cost of treatment; not paid separately",
}

# list name -> [(item, category)], numbered in this order from 1.
_ITEMS: dict[str, list[tuple[str, str]]] = {
    "I": [
        ("Baby food", "food"),
        ("Carry bags", "personal"),
        ("Laundry charges", "services"),
        ("Mineral water", "food"),
        ("Telephone charges", "services"),
        ("Television charges", "services"),
        ("Attendant charges", "services"),
        ("Attendant food", "food"),
        ("Medical records charges", "administrative"),
        ("Photocopy charges", "administrative"),
        ("Gloves", "consumables"),
        ("Mask", "consumables"),
        ("Diaper", "personal"),
        ("Thermometer", "devices"),
    ],
    "II": [
        ("Admission kit", "personal"),
        ("Patient gown", "personal"),
        ("Slippers", "personal"),
        ("Tooth brush", "personal"),
        ("Bed pan", "personal"),
        ("Housekeeping charges", "services"),
        ("Air conditioner charges", "services"),
        ("Patient identification band", "administrative"),
    ],
    "III": [
        ("Gauze", "consumables"),
        ("Cotton", "consumables"),
        ("Surgical tape", "consumables"),
        ("Surgical blade", "consumables"),
        ("Disposable razor", "consumables"),
        ("Theatre booking charges", "administrative"),
    ],
    "IV": [
        ("Registration charges", "administrative"),
        ("Infusion pump charges", "devices"),
        ("Diet charges", "food"),
        ("Alcohol swabs", "consumables"),
        ("Glucometer strips", "consumables"),
        ("Urine bag", "consumables"),
    ],
}

# (list name, item number, item, category, condition)
NON_PAYABLE_ITEMS: list[tuple[str, int, str, str, str]] = [
    (list_name, number, item, category, _CONDITIONS[list_name])
    for list_name, items in _ITEMS.items()
    for number, (item, category) in enumerate(items, start=1)
]
