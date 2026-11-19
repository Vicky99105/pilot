import base64


def data_url(image: bytes) -> str:
    return f"data:{_mime(image)};base64,{base64.b64encode(image).decode()}"


def _mime(image: bytes) -> str:
    if image.startswith(b"\x89PNG"):
        return "image/png"
    if image.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def retry_instruction(error: str) -> str:
    return (
        "The previous reply did not match the JSON schema. Validation errors:\n"
        f"{error[:2000]}\n"
        "Reply again with only a JSON object that matches the schema."
    )
