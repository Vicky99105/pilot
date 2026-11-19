"""Pure code-range matching, shared by the knowledge base and the rules engine. No I/O."""


def range_key(code: str) -> str:
    return code.strip().upper().rstrip("-").replace(".", "")


def is_span(item: str) -> bool:
    """True for two codes joined by a hyphen, such as K35-K38. K35.- and K35- are one code
    with the codes below it."""
    low, sep, high = item.strip().partition("-")
    return bool(sep and low and high.strip())


def code_in_ranges(code: str, ranges: list[str]) -> bool:
    """True when the code falls in any range. A range is one code, which covers the codes
    below it ("K35", "K35.-"), or two codes joined by a hyphen ("K35-K38"), which covers both
    ends and everything below them in code order."""
    key = range_key(code)
    for item in ranges:
        low, _, high = item.partition("-") if is_span(item) else (item, "", item)
        low, high = range_key(low), range_key(high)
        if low <= key and (key <= high or key.startswith(high)):
            return True
    return False


def has_prefix(code: str, prefixes: list[str]) -> bool:
    """True when an ICD-10-PCS code starts with any of the prefixes."""
    key = code.strip().upper()
    return any(key.startswith(p.strip().upper()) for p in prefixes if p.strip())
