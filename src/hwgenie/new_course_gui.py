"""Browser wizard for creating a new course.

Two ways in, one page:

- embedded in the hwGenie app (grade_gui serves ``/new-course`` on its
  own server, reached from the launcher/picker page);
- standalone via ``hwgenie new-course --gui``, which serves the same
  page on an ephemeral port.

The creation pipeline runs in a background thread; the page polls
``/new-course/status`` for progress lines.
"""

from __future__ import annotations

import html
import json
import threading
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .new_course import (CreateRequest, CreateResult, create_course,
                         load_defaults)
from .themes import THEMES
from .webstyle import BASE_CSS


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.lines: list[str] = []
        self.phase = "idle"          # idle | running | done | error
        self.result: dict | None = None
        self.shutdown = threading.Event()

    def log(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)

    def snapshot(self) -> dict:
        with self.lock:
            return {"phase": self.phase, "lines": list(self.lines),
                    "result": self.result}


STATE = _State()


def _worker(req: CreateRequest) -> None:
    try:
        result = create_course(req, STATE.log)
    except Exception as e:   # noqa: BLE001 — a dead worker must never
        # leave the page saying "working…" forever
        result = CreateResult(ok=False, error=f"unexpected error: {e!r}")
    with STATE.lock:
        STATE.result = asdict(result)
        STATE.phase = "done" if result.ok else "error"


def start_create(data: dict) -> dict:
    """Kick off course creation from a parsed request body.

    Shared by the standalone wizard server and the hwGenie app server.
    """
    with STATE.lock:
        if STATE.phase == "running":
            return {"ok": False, "error": "already running"}
        STATE.phase = "running"
        STATE.lines.clear()
        STATE.result = None
    req = CreateRequest(
        course=data.get("course", ""),
        title=data.get("title", ""),
        semester=data.get("semester", ""),
        instructor=data.get("instructor", ""),
        office=data.get("office", ""),
        email=data.get("email", ""),
        theme=data.get("theme", "slate"),
        repo=data.get("repo", ""),
        parent_dir=data.get("parent_dir", ""),
        deploy=bool(data.get("deploy", True)),
        wait_for_build=bool(data.get("wait_for_build", True)),
        use_problem_sets=bool(data.get("use_problem_sets", True)),
        use_lessons=bool(data.get("use_lessons", True)),
        use_readings=bool(data.get("use_readings", False)),
    )
    threading.Thread(target=_worker, args=(req,), daemon=True).start()
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence request logging
        pass

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html", "/new-course"):
            self._send(render_wizard(embedded=False).encode("utf-8"))
        elif self.path == "/new-course/status":
            self._send(json.dumps(STATE.snapshot()).encode("utf-8"),
                       "application/json")
        elif self.path in ("/icon-192.png", "/icon-512.png"):
            from .appicon import icon_png
            size = 192 if "192" in self.path else 512
            self._send(icon_png(size), "image/png")
        else:
            self._send(b"not found", code=404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/new-course/create":
            self._send(json.dumps(start_create(data)).encode("utf-8"),
                       "application/json")
        elif self.path == "/quit":
            self._send(b'{"ok": true}', "application/json")
            STATE.shutdown.set()
        else:
            self._send(b"not found", code=404)


def serve_wizard(port: int = 0) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"hwgenie new-course wizard: {url}")
    print("(Leave this window open; it closes when you click "
          "'All done' in the browser, or press Ctrl-C.)")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webbrowser.open(url)
    try:
        STATE.shutdown.wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    print("Wizard closed.")
    return 0


# --------------------------------------------------------------- page --

def render_wizard(embedded: bool) -> str:
    """The wizard page.

    ``embedded=True`` when served inside the hwGenie app: adds a way
    back to the launcher and keep-alive pings for --auto-exit; the
    standalone wizard instead gets an 'All done' button that shuts its
    own server down.
    """
    from .appicon import LAMP_SVG
    from .webstyle import nav_header
    d = load_defaults()
    parent = d.get("parent_dir") or str(Path.home())
    theme_options = "".join(
        f'<option value="{name}">{name}</option>' for name in THEMES)
    esc = lambda k: html.escape(d.get(k, ""), quote=True)  # noqa: E731
    # embedded: the shared app-nav header carries the branding; the
    # standalone wizard keeps its own hwGenie h1
    # the wizard is reached from the Courses page, so that tab stays lit
    nav = nav_header("courses") if embedded else ""
    masthead = "" if embedded else "<h1>hwGenie __LAMP__</h1>"
    return PAGE.replace("__THEMES__", theme_options) \
               .replace("__INSTRUCTOR__", esc("instructor")) \
               .replace("__OFFICE__", esc("office")) \
               .replace("__EMAIL__", esc("email")) \
               .replace("__PARENT__", html.escape(parent, quote=True)) \
               .replace("__NAV__", nav) \
               .replace("__MASTHEAD__", masthead) \
               .replace("__LAMP__", LAMP_SVG) \
               .replace("__MODE__", "embedded" if embedded else "standalone")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>New course &mdash; hwGenie</title>
<link rel="icon" href="/icon-192.png">
<meta name="theme-color" content="#24589f">
<style>
__BASE__
  /* height:auto so the body box spans the full content: the sticky
     .appnav sticks for the whole scroll, not just the first viewport */
  html, body { height: auto; min-height: 100%; }
  body { overflow: auto; display: block; }
  main { max-width: 620px; margin: 0 auto; padding: 1.75rem 1.25rem 4rem; }
  h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
  .lamp { height: .72em;  /* cap height: lamp tip = top of G */ width: auto; color: var(--accent);
          vertical-align: baseline; margin-left: .2rem; }
  .sub { color: var(--muted); margin: 0 0 1.75rem; }
  h2 { font-size: .95rem; letter-spacing: .04em; text-transform: uppercase;
       color: var(--muted); margin: 1.6rem 0 .5rem; }
  .card { background: var(--card-bg); padding: .9rem 1.1rem 1.1rem; }
  label { display: block; margin: .7rem 0 .2rem; font-size: .9rem; }
  .card > .row:first-child label, .card > label:first-child { margin-top: 0; }
  .hint { color: var(--muted); font-size: .8rem; margin-top: .15rem; }
  input[type=text], input[type=email], input[type=number], select {
    width: 100%; padding: .45rem .6rem; font: inherit;
    color: var(--fg); background: var(--bg);
    border: 1px solid var(--border);
  }
  input:focus, select:focus { outline: 2px solid var(--accent); border-color: transparent; }
  .row { display: flex; gap: .75rem; }
  .row > div { flex: 1; }
  .check { display: flex; align-items: baseline; gap: .5rem; margin-top: .8rem; }
  .check label { margin: 0; display: inline; }
  #go {
    font: inherit; font-size: 1rem; padding: .55rem 1.5rem; cursor: pointer;
    border: none; background: var(--accent); color: var(--bg);
    margin-top: 1.4rem;
  }
  #go:hover { background: color-mix(in srgb, var(--accent) 85%, var(--fg)); }
  #go:disabled { opacity: .45; cursor: default; }
  #repopreview {
    font-family: ui-monospace, Menlo, monospace; font-size: .9rem;
  }
  #log {
    display: none; background: var(--code-bg);
    padding: 1rem 1.1rem; margin-top: 1.25rem;
    font-family: ui-monospace, Menlo, monospace; font-size: .8rem;
    white-space: pre-wrap; max-height: 40vh; overflow-y: auto;
  }
  #summary { display: none; margin-top: 1.5rem; }
  #summary .ok { color: var(--sol-accent); font-weight: bold; }
  #summary .bad { color: var(--alert); font-weight: bold; }
  #summary button.ghost { font-size: .95rem; padding: .45rem 1rem; }
  ol li { margin: .35rem 0; }
  .spin { color: var(--muted); font-size: .9rem; }
</style>
</head>
<body>
__NAV__
<main>
  __MASTHEAD__
  <p class="sub">Set up a new course: a private GitHub repo made from
  the course template, filled with your course data, website build
  switched on. Afterwards you just import the repo into Overleaf.</p>

  <form id="f" onsubmit="return false;">
  <h2>Course</h2>
  <div class="card">
    <div class="row">
      <div>
        <label for="course">Course code</label>
        <input type="text" id="course" placeholder="Math 301" required>
      </div>
      <div>
        <label for="title">Course title</label>
        <input type="text" id="title" placeholder="Real Analysis" required>
      </div>
    </div>
    <div class="row">
      <div>
        <label for="term">Term</label>
        <select id="term">
          <option>Fall</option><option>Spring</option>
          <option>Summer</option><option>Winter</option>
        </select>
      </div>
      <div>
        <label for="year">Year</label>
        <input type="number" id="year" min="2020" max="2100">
      </div>
    </div>
  </div>

  <h2>Syllabus header</h2>
  <div class="card">
    <label for="instructor">Instructor</label>
    <input type="text" id="instructor" value="__INSTRUCTOR__" placeholder="Prof. Ada Lovelace">
    <div class="row">
      <div>
        <label for="office">Office</label>
        <input type="text" id="office" value="__OFFICE__" placeholder="123 Main Building">
      </div>
      <div>
        <label for="email">Email</label>
        <input type="email" id="email" value="__EMAIL__" placeholder="you@school.edu">
      </div>
    </div>
  </div>

  <h2>Repository &amp; site</h2>
  <div class="card">
    <label for="repopreview">GitHub repo name</label>
    <input type="text" id="repopreview" spellcheck="false">
    <div class="hint">Auto-filled from course + term; edit if you like.
      Becomes github.com/&lt;you&gt;/<span id="repoecho"></span></div>
    <label for="parent">Local folder (the repo is cloned inside it)</label>
    <input type="text" id="parent" value="__PARENT__" spellcheck="false">
    <label for="theme">Site theme</label>
    <select id="theme">__THEMES__</select>
    <label>Sections</label>
    <div class="check">
      <input type="checkbox" id="psets" checked>
      <label for="psets">Problem sets</label>
    </div>
    <div class="check">
      <input type="checkbox" id="lessons" checked>
      <label for="lessons">Lessons</label>
    </div>
    <div class="check">
      <input type="checkbox" id="readings">
      <label for="readings">Reading assignments</label>
    </div>
    <div class="hint" style="margin-left:1.6rem">Handouts and the syllabus
      are always included. Uncheck a section this course won&rsquo;t use &mdash;
      its folder and home-page section are left out entirely. Reading
      assignments (textbook links with due dates, edited in
      <code>readings.tex</code>) are off unless checked.</div>
    <div class="check">
      <input type="checkbox" id="deploy" checked>
      <label for="deploy">Publish the site right away</label>
    </div>
    <div class="hint" style="margin-left:1.6rem">Uncheck to keep the site
      offline until you flip a switch later (SETUP.md explains how).</div>
    <div class="check">
      <input type="checkbox" id="wait" checked>
      <label for="wait">Wait for the first build and report the result</label>
    </div>
    <div class="hint" style="margin-left:1.6rem">The first build takes
      5&ndash;10 minutes (TeX Live install). You can uncheck and watch the
      repo&rsquo;s Actions tab instead.</div>
  </div>

  <button id="go">Create course</button>
  <span id="busy" class="spin" style="display:none">&nbsp; working&hellip;</span>
  </form>

  <div id="log"></div>

  <div id="summary">
    <p id="verdict"></p>
    <p id="links"></p>
    <p><strong>Next steps:</strong></p>
    <ol id="steps"></ol>
    <button class="ghost" id="quit"></button>
  </div>
</main>
<script>
  "use strict";
  const MODE = "__MODE__";   // embedded (inside hwGenie) | standalone
  const $ = id => document.getElementById(id);
  $("year").value = new Date().getFullYear();
  $("quit").textContent = MODE === "embedded"
    ? "All done — back to hwGenie" : "All done — close the wizard";
  let repoTouched = false;

  function slug(s) { return s.toLowerCase().replace(/[^a-z0-9]/g, ""); }
  function autoRepo() {
    if (repoTouched) return;
    const name = slug($("course").value) + "-" +
                 slug($("term").value) + $("year").value;
    $("repopreview").value = ($("course").value ? name : "");
    $("repoecho").textContent = $("repopreview").value;
  }
  ["course", "term", "year"].forEach(id =>
    $(id).addEventListener("input", autoRepo));
  $("repopreview").addEventListener("input", () => {
    repoTouched = true;
    $("repoecho").textContent = $("repopreview").value;
  });

  let poll = null;
  $("go").addEventListener("click", async () => {
    if (!$("course").value || !$("title").value) {
      alert("Course code and title are required."); return;
    }
    $("go").disabled = true;
    $("busy").style.display = "inline";
    $("log").style.display = "block";
    const body = {
      course: $("course").value.trim(),
      title: $("title").value.trim(),
      semester: $("term").value + " " + $("year").value,
      instructor: $("instructor").value.trim(),
      office: $("office").value.trim(),
      email: $("email").value.trim(),
      theme: $("theme").value,
      repo: $("repopreview").value.trim(),
      parent_dir: $("parent").value.trim(),
      deploy: $("deploy").checked,
      wait_for_build: $("wait").checked,
      use_problem_sets: $("psets").checked,
      use_lessons: $("lessons").checked,
      use_readings: $("readings").checked,
    };
    await fetch("/new-course/create",
                {method: "POST", body: JSON.stringify(body)});
    poll = setInterval(refresh, 1000);
  });

  async function refresh() {
    const s = await (await fetch("/new-course/status")).json();
    $("log").textContent = s.lines.join("\n");
    $("log").scrollTop = $("log").scrollHeight;
    if (s.phase === "done" || s.phase === "error") {
      clearInterval(poll);
      $("busy").style.display = "none";
      showSummary(s.result);
    }
  }

  function showSummary(r) {
    $("summary").style.display = "block";
    if (r.ok) {
      $("verdict").innerHTML = '<span class="ok">Course created.</span>' +
        (r.build_state === "success" ? " First build succeeded." :
         r.build_state === "running" ? " First build still running (check the Actions tab)." : "");
      let links = 'Repo: <a href="' + r.repo_url + '" target="_blank">' +
                  r.repo_url + "</a><br>Local copy: <code>" +
                  r.local_path + "</code>";
      if (r.site_url) links += '<br>Site: <a href="' + r.site_url +
                  '" target="_blank">' + r.site_url + "</a>";
      $("links").innerHTML = links;
      $("steps").innerHTML = r.next_steps.map(s => "<li>" + s + "</li>").join("");
    } else {
      $("verdict").innerHTML = '<span class="bad">Something went wrong:</span> ' + r.error;
      $("links").textContent = "";
      $("steps").innerHTML = "<li>Fix the issue above and try again — " +
        "it is safe to re-run after deleting any half-created repo.</li>";
      $("go").disabled = false;
    }
    window.scrollTo(0, document.body.scrollHeight);
  }

  $("quit").addEventListener("click", async () => {
    if (MODE === "embedded") { location.href = "/"; return; }
    await fetch("/quit", {method: "POST", body: "{}"});
    document.body.innerHTML = "<main><h1>Wizard closed.</h1>" +
      "<p class=sub>You can close this tab.</p></main>";
  });

  if (MODE === "embedded") {
    // keep the hwGenie server's --auto-exit watchdog fed while this
    // tab is open (same protocol as the grader/picker pages)
    setInterval(() => {
      fetch("/ping", {method: "POST", body: "{}"}).catch(() => {});
    }, 2000);
    addEventListener("pagehide", () => {
      try { navigator.sendBeacon("/bye", "{}"); } catch (e) {}
    });
  }

  autoRepo();
</script>
</body>
</html>
""".replace("__BASE__", BASE_CSS)
