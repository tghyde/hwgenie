"""Local browser wizard for ``hwgenie new-course --gui``.

Serves a single form page on localhost, runs the creation pipeline in a
background thread, and streams progress to the page via polling.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .new_course import CreateRequest, create_course, load_defaults
from .themes import THEMES


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
    result = create_course(req, STATE.log)
    with STATE.lock:
        STATE.result = asdict(result)
        STATE.phase = "done" if result.ok else "error"


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
        if self.path in ("/", "/index.html"):
            self._send(render_page().encode("utf-8"))
        elif self.path == "/status":
            self._send(json.dumps(STATE.snapshot()).encode("utf-8"),
                       "application/json")
        else:
            self._send(b"not found", code=404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/create":
            with STATE.lock:
                if STATE.phase == "running":
                    self._send(b'{"ok": false, "error": "already running"}',
                               "application/json")
                    return
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
            )
            threading.Thread(target=_worker, args=(req,), daemon=True).start()
            self._send(b'{"ok": true}', "application/json")
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

def render_page() -> str:
    d = load_defaults()
    parent = d.get("parent_dir") or str(Path.home())
    theme_options = "".join(
        f'<option value="{name}">{name}</option>' for name in THEMES)
    return PAGE.replace("__THEMES__", theme_options) \
               .replace("__INSTRUCTOR__", d.get("instructor", "")) \
               .replace("__OFFICE__", d.get("office", "")) \
               .replace("__EMAIL__", d.get("email", "")) \
               .replace("__PARENT__", parent)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>New course — hwgenie</title>
<style>
  :root {
    --bg: #faf9f6; --fg: #20242a; --muted: #5d646f; --accent: #24589f;
    --alert: #b3223a; --border: #dcdad0; --card-bg: #efeee8;
    --sol-accent: #2c6a3f; --code-bg: #f1f0ea; --hover-bg: #e2e8f3;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #15171c; --fg: #e7e5e0; --muted: #9aa1ad; --accent: #8db1ea;
      --alert: #e87a90; --border: #33363e; --card-bg: #1f222a;
      --sol-accent: #98cda5; --code-bg: #22252d; --hover-bg: #2b3242;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 17px/1.55 Charter, "Bitstream Charter", Georgia, serif;
  }
  main { max-width: 620px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
  h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
  .sub { color: var(--muted); margin: 0 0 1.75rem; }
  fieldset {
    border: none; background: var(--card-bg); border-radius: 10px;
    padding: 1.1rem 1.25rem 1.25rem; margin: 0 0 1.1rem;
  }
  legend {
    font-weight: bold; padding: 0 .4rem; margin-left: -.4rem;
    letter-spacing: .04em;
  }
  label { display: block; margin: .7rem 0 .2rem; font-size: .95rem; }
  .hint { color: var(--muted); font-size: .8rem; margin-top: .15rem; }
  input[type=text], input[type=email], input[type=number], select {
    width: 100%; padding: .5rem .6rem; font: inherit; font-size: 1rem;
    color: var(--fg); background: var(--bg);
    border: 1px solid var(--border); border-radius: 6px;
  }
  input:focus, select:focus { outline: 2px solid var(--accent); border-color: transparent; }
  .row { display: flex; gap: .75rem; }
  .row > div { flex: 1; }
  .check { display: flex; align-items: baseline; gap: .5rem; margin-top: .8rem; }
  .check label { margin: 0; display: inline; }
  .check .hint { margin-left: 1.6rem; }
  button {
    font: inherit; font-size: 1.05rem; padding: .6rem 1.6rem;
    border-radius: 8px; border: 1px solid var(--accent);
    background: var(--accent); color: var(--bg); cursor: pointer;
  }
  button:hover { background: var(--bg); color: var(--accent); }
  button:disabled { opacity: .45; cursor: default; }
  button.ghost { background: transparent; color: var(--accent); }
  button.ghost:hover { background: var(--hover-bg); }
  #repopreview {
    font-family: ui-monospace, Menlo, monospace; font-size: .95rem;
  }
  #log {
    display: none; background: var(--code-bg); border: 1px solid var(--border);
    border-radius: 10px; padding: 1rem 1.1rem; margin-top: 1.25rem;
    font-family: ui-monospace, Menlo, monospace; font-size: .8rem;
    white-space: pre-wrap; max-height: 40vh; overflow-y: auto;
  }
  #summary { display: none; margin-top: 1.5rem; }
  #summary .ok { color: var(--sol-accent); font-weight: bold; }
  #summary .bad { color: var(--alert); font-weight: bold; }
  #summary a { color: var(--accent); }
  ol li { margin: .35rem 0; }
  .spin { color: var(--muted); font-size: .9rem; }
</style>
</head>
<body>
<main>
  <h1>Set up a new course</h1>
  <p class="sub">Creates a private GitHub repo from the course template,
  fills in your course data, and turns on the website build.
  Afterwards you just import the repo into Overleaf.</p>

  <form id="f" onsubmit="return false;">
  <fieldset>
    <legend>Course</legend>
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
  </fieldset>

  <fieldset>
    <legend>Syllabus header</legend>
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
  </fieldset>

  <fieldset>
    <legend>Repository &amp; site</legend>
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
    <div class="hint" style="margin-left:1.6rem">Handouts and the syllabus
      are always included. Uncheck a section this course won&rsquo;t use —
      its folder and home-page section are left out entirely.</div>
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
  </fieldset>

  <button id="go">Create course</button>
  <span id="busy" class="spin" style="display:none">&nbsp; working&hellip;</span>
  </form>

  <div id="log"></div>

  <div id="summary">
    <p id="verdict"></p>
    <p id="links"></p>
    <p><strong>Next steps:</strong></p>
    <ol id="steps"></ol>
    <button class="ghost" id="quit">All done — close the wizard</button>
  </div>
</main>
<script>
  const $ = id => document.getElementById(id);
  $("year").value = new Date().getFullYear();
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
    };
    await fetch("/create", {method: "POST", body: JSON.stringify(body)});
    poll = setInterval(refresh, 1000);
  });

  async function refresh() {
    const s = await (await fetch("/status")).json();
    $("log").textContent = s.lines.join("\\n");
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
      $("steps").innerHTML = "<li>Fix the issue above and try again \\u2014 " +
        "it is safe to re-run after deleting any half-created repo.</li>";
      $("go").disabled = false;
    }
    window.scrollTo(0, document.body.scrollHeight);
  }

  $("quit").addEventListener("click", async () => {
    await fetch("/quit", {method: "POST", body: "{}"});
    document.body.innerHTML = "<main><h1>Wizard closed.</h1>" +
      "<p class=sub>You can close this tab.</p></main>";
  });

  autoRepo();
</script>
</body>
</html>
"""
