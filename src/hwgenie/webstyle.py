"""Shared base CSS for the hwGenie web pages (grader, picker, wizard).

Flat look, matching the course pages: rectangular blocks of card-bg on
bg, no borders, no rounded corners.
"""

BASE_CSS = r"""
  :root {
    --bg: #faf9f6; --fg: #20242a; --muted: #5d646f; --accent: #24589f;
    --alert: #b3223a; --border: #dcdad0; --card-bg: #efeee8;
    --sol-accent: #2c6a3f; --code-bg: #f1f0ea; --hover-bg: #e2e8f3;
    --draft-bg: #f3ecf8; --draft-accent: #7b4ea3; --mark-bg: #ffd76e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #15171c; --fg: #e7e5e0; --muted: #9aa1ad; --accent: #8db1ea;
      --alert: #e87a90; --border: #33363e; --card-bg: #1f222a;
      --sol-accent: #98cda5; --code-bg: #22252d; --hover-bg: #2b3242;
      --draft-bg: #2a2233; --draft-accent: #c9a6e8; --mark-bg: #8a6d1d;
    }
  }
  * { box-sizing: border-box; }
  :root {
    /* bars (header, sticky navs, panel heads) get a faint accent tint so
       they read as chrome, not cards */
    --bar-bg: color-mix(in srgb, var(--accent) 10%, var(--bg));
  }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    display: flex; flex-direction: column;
    overflow: hidden;   /* the app fills the viewport; panes scroll */
  }
  button { font: inherit; }
  button.ghost {
    font-size: .85rem; padding: .25rem .7rem; cursor: pointer;
    border: none; background: transparent; color: var(--accent);
  }
  button.ghost:hover { background: var(--hover-bg); }
  button.ghost.active { background: var(--hover-bg); }
  /* mouse-first app with letter shortcuts: focus rings on clicked
     buttons only linger and distract */
  button:focus { outline: none; }
  .sp { flex: 1; }
  a { color: var(--accent); }
"""
