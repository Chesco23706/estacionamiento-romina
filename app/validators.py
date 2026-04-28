import re


def clean_text(value, max_length, field_name):
    normalized = " ".join((value or "").strip().split())
    if not normalized:
        raise ValueError(f"El campo {field_name} es obligatorio.")
    if len(normalized) > max_length:
        raise ValueError(
            f"El campo {field_name} no puede exceder {max_length} caracteres."
        )
    return normalized


def clean_optional_text(value, max_length):
    normalized = " ".join((value or "").strip().split())
    if len(normalized) > max_length:
        raise ValueError(f"El texto no puede exceder {max_length} caracteres.")
    return normalized


def clean_ticket(value):
    normalized = clean_text(value, 30, "número de ticket")
    if not re.fullmatch(r"[A-Za-z0-9\-_]+", normalized):
        raise ValueError(
            "El número de ticket solo puede usar letras, números, guion y guion bajo."
        )
    return normalized.upper()


def clean_plate(value):
    normalized = clean_text(value, 50, "placas o identificador")
    if not re.fullmatch(r"[A-Za-z0-9\-\s]+", normalized):
        raise ValueError(
            "Las placas o identificador solo pueden usar letras, números, espacios y guiones."
        )
    return normalized.upper()


def validate_password_strength(password):
    if len(password or "") < 10:
        raise ValueError("La contraseña debe tener al menos 10 caracteres.")
    if not re.search(r"[A-Z]", password):
        raise ValueError("La contraseña debe incluir al menos una letra mayúscula.")
    if not re.search(r"[a-z]", password):
        raise ValueError("La contraseña debe incluir al menos una letra minúscula.")
    if not re.search(r"[0-9]", password):
        raise ValueError("La contraseña debe incluir al menos un número.")
    if not re.search(r"[^A-Za-z0-9]", password):
        raise ValueError("La contraseña debe incluir al menos un símbolo.")
    return password
