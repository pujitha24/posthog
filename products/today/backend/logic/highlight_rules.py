def human(value: float) -> str:
    size = abs(value)
    if size >= 999_500:
        return f"{value / 1_000_000:.1f}M"
    if size >= 9_950:
        return f"{value / 1000:.0f}k"
    if size >= 999.5:
        return f"{value / 1000:.1f}k"
    return f"{value:.0f}" if size >= 10 or float(value).is_integer() else f"{value:.1f}"


def exact(value: float) -> str:
    return f"{value:,.0f}" if abs(value) < 100_000 else human(value)
