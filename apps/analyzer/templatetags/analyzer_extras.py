from django import template

register = template.Library()


@register.filter
def excel_column(number):
    """Return the spreadsheet column letter for a 1-based index (1 -> A, 27 -> AA)."""
    try:
        number = int(number)
    except (TypeError, ValueError):
        return ""
    letters = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


@register.filter
def humanize_key(value):
    """Turn a model-supplied key such as "bar_chart" into "Bar chart"."""
    text = str(value or "").replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:]
