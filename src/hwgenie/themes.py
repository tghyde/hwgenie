"""Color/typography themes.

Every visual color in the templates flows through CSS custom properties, so a
theme is just a dict of variable values (light + dark) plus a font stack.
Course repos pick a theme in course.yml (``theme: slate``) and may override
individual values with dotted keys::

    theme: slate
    theme.light.accent: "#8c2f22"
    theme.dark.accent: "#d98d75"
    theme.font-body: "Palatino, serif"
"""

from __future__ import annotations

from typing import Dict, Optional

SLATE = {
    "light": {
        "bg": "#faf9f6",
        "fg": "#20242a",
        "muted": "#5d646f",
        "accent": "#24589f",
        "alert": "#b3223a",
        "border": "#dcdad0",
        "card-bg": "#efeee8",
        "sol-bg": "#e6efe6",
        "sol-accent": "#2c6a3f",
        "code-bg": "#f1f0ea",
        "hover-bg": "#e2e8f3",
    },
    "dark": {
        "bg": "#15171c",
        "fg": "#e7e5e0",
        "muted": "#9aa1ad",
        "accent": "#8db1ea",
        "alert": "#e87a90",
        "border": "#33363e",
        "card-bg": "#1f222a",
        "sol-bg": "#1c2721",
        "sol-accent": "#98cda5",
        "code-bg": "#22252d",
        "hover-bg": "#2b3242",
    },
    "font-body": 'Charter, "Bitstream Charter", Georgia, "Times New Roman", serif',
}

THEMES: Dict[str, dict] = {"slate": SLATE}


def _vars_block(values: Dict[str, str]) -> str:
    return "\n".join(f"  --{k}: {v};" for k, v in values.items())


def theme_css(name: str = "slate", overrides: Optional[Dict[str, str]] = None) -> str:
    """CSS :root blocks for a theme.

    `overrides` uses dotted keys relative to the theme: "light.accent",
    "dark.bg", "font-body".
    """
    base = THEMES.get(name.lower())
    if base is None:
        raise KeyError(
            f"Unknown theme {name!r}; available: {', '.join(sorted(THEMES))}"
        )
    light = dict(base["light"])
    dark = dict(base["dark"])
    font = base["font-body"]
    for key, value in (overrides or {}).items():
        parts = key.split(".", 1)
        if key == "font-body":
            font = value
        elif len(parts) == 2 and parts[0] == "light":
            light[parts[1]] = value
        elif len(parts) == 2 and parts[0] == "dark":
            dark[parts[1]] = value
    light["font-body"] = font
    return (
        f":root {{\n{_vars_block(light)}\n}}\n"
        f"@media (prefers-color-scheme: dark) {{\n"
        f"  :root:not([data-theme=\"light\"]) {{\n{_vars_block(dark)}\n  }}\n}}\n"
        f":root[data-theme=\"dark\"] {{\n{_vars_block(dark)}\n}}\n"
    )


def theme_from_config(cfg: Dict[str, str]) -> str:
    """Build theme CSS from a parsed course.yml dict."""
    name = cfg.get("theme", "slate")
    overrides = {
        k[len("theme."):]: v for k, v in cfg.items() if k.startswith("theme.")
    }
    return theme_css(name, overrides)
