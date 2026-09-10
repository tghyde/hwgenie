"""The instructor how-to page: the Moodle round trip, extra credit,
and the external grading server, served at /grading/howto (local app
only — the page documents instructor-only features, so grader-only
servers 404 it)."""

from __future__ import annotations

from .webstyle import BASE_CSS, nav_header


def render_howto() -> str:
    from .appicon import LAMP_SVG
    return (HOWTO_PAGE.replace("__NAV__", nav_header("grading"))
                      .replace("__LAMP__", LAMP_SVG))


HOWTO_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hwGenie — grading how-to</title>
<link rel="icon" href="/icon-192.png">
<meta name="theme-color" content="#24589f">
<style>
__BASE__
  html, body { height: auto; min-height: 100%; }
  body { overflow: auto; display: block; }
  main { max-width: 680px; margin: 0 auto; padding: 1.75rem 1.25rem 5rem; }
  h1 { font-size: 1.35rem; margin: 0 0 .3rem; }
  .sub { color: var(--muted); margin: 0 0 1.4rem; }
  h2 { font-size: 1.02rem; letter-spacing: .05em; margin: 2.4rem 0 .7rem;
       padding-top: 1.3rem; border-top: 1px solid var(--border); }
  h3 { font-size: .92rem; margin: 1.3rem 0 .4rem; }
  p, li { line-height: 1.55; font-size: .92rem; }
  ol, ul { padding-left: 1.4rem; }
  li { margin: .35rem 0; }
  code { background: var(--code-bg); padding: .08rem .3rem;
         font-size: .85em; }
  .toc { background: var(--card-bg); padding: .8rem 1rem; }
  .toc a { color: var(--accent); text-decoration: none; }
  .toc a:hover { background: var(--hover-bg); }
  .toc li { margin: .2rem 0; }
  .note { background: var(--card-bg); border-left: 3px solid var(--accent);
          padding: .6rem .9rem; margin: .8rem 0; font-size: .88rem; }
  .warn { border-left-color: var(--alert); }
  b.ui { font-weight: 600; }
  [id] { scroll-margin-top: 4rem; }
</style>
</head>
<body>
__NAV__
<main>
  <h1>Grading how-to</h1>
  <p class="sub">The full round trip: Moodle &rarr; hwGenie &rarr; the
  graders &rarr; back to Moodle.</p>

  <div class="toc"><ul>
    <li><a href="#collect">1. Getting homework out of Moodle</a></li>
    <li><a href="#rubric">2. Setting up the rubric (and extra
      credit)</a></li>
    <li><a href="#server">3. Grading on the external server</a></li>
    <li><a href="#export">4. Exporting</a></li>
    <li><a href="#moodle">5. Returning everything to Moodle</a></li>
    <li><a href="#ec">6. Extra credit in the Moodle gradebook</a></li>
    <li><a href="#trouble">Troubleshooting</a></li>
  </ul></div>

  <h2 id="collect">1. Getting homework out of Moodle</h2>
  <ol>
    <li>In the Moodle assignment, open <b class="ui">View all
      submissions</b>.</li>
    <li>From the <b class="ui">Grading action</b> dropdown, choose
      <b class="ui">Download all submissions</b> &mdash; you get one big
      zip.</li>
    <li>Same dropdown, also choose <b class="ui">Download grading
      worksheet</b> &mdash; a <code>Grades-&hellip;.csv</code> file.
      (If you don&rsquo;t see the worksheet options, enable
      <b class="ui">Offline grading worksheet</b> and <b class="ui">
      Feedback files</b> under the assignment&rsquo;s Feedback types
      settings.)</li>
    <li>Make a folder for the assignment, e.g.
      <code>grading-lab/math221/ps01/</code> &mdash; easiest via
      <b class="ui">New assignment</b> under <b class="ui">Collect from
      Moodle</b> on the Grading tab: pick or name the course folder, name
      the problem set, then <b class="ui">Add files&hellip;</b> picks the
      zip, worksheet and .tex straight from Downloads and files them
      for you &mdash; with the zip and worksheet
      in <code>moodle-raw/</code> and the assignment&rsquo;s .tex in
      <code>build/</code> &mdash; the <em>source</em> file (e.g.
      <code>ps01.tex</code> from the course repo) if graders should see
      your solutions in the problem pane, or the blank submission
      template students downloaded if they shouldn&rsquo;t. Solutions
      never reach the student feedback either way.</li>
    <li>Collect, in Terminal from the <code>hwgenie</code> folder:
      <code>.venv/bin/hwgenie collect "&lt;zip&gt;" --dest
      "&lt;assignment&gt;/grading" --template "&lt;template.tex&gt;"</code>.
      The template enables the problem-statement pane and the
      solution-box count check; the report flags students who submitted
      no tex, didn&rsquo;t use the template, or uploaded several PDFs
      (pick the right one by copying it to
      <code>submissions/&lt;slug&gt;/submission.pdf</code>).
      Add <code>--due "2026-09-04 23:59"</code> (course time) so late
      work is flagged &mdash; see <a href="#late">Late work</a>.
      <em>Or skip Terminal:</em> paste the zip&rsquo;s path (and the due
      date) into <b class="ui">Collect from Moodle</b> on the Grading
      tab &mdash; with the layout above it finds the worksheet and the
      template itself.</li>
    <li><em>Late work arriving afterwards:</em> download the zip (and
      worksheet) from Moodle again and re-run the same
      <code>collect</code> command. Existing students are left exactly as
      they were; new ones are added and tagged &ldquo;added by a later
      collect&rdquo;; a student who re-uploaded after grading began is
      flagged but not replaced. The &#x21bb; button on the
      assignment&rsquo;s row on the Grading tab does the same re-collect
      with one click. Then push again (section 3) &mdash; a re-push
      never touches the graders&rsquo; work.</li>
    <li>Drop the <code>Grades-&hellip;.csv</code> worksheet into the
      grading folder (next to <code>manifest.json</code>). It carries the
      students&rsquo; email addresses and is what fills grades back into
      Moodle later.</li>
    <li>Write <code>rubric.yml</code> in the grading folder (next
      section) &mdash; without one every part is &ldquo;Part k&rdquo; out
      of 5.</li>
  </ol>

  <h2 id="rubric">2. Setting up the rubric (and extra credit)</h2>
  <p>The grading folder gets a <code>rubric.yml</code> with one line per
  solution box. Edit the labels&rsquo; point values to taste; the app
  picks changes up on restart (or re-push):</p>
  <ul>
    <li><code>- 2.3: 4</code> &mdash; part 2.3, out of 4 points.</li>
    <li><code>- 2.5: 3 ec</code> &mdash; an <b>extra credit</b> part: a
      trailing <code>ec</code> keeps its points out of the assignment
      total (Moodle refuses grades above an assignment&rsquo;s maximum),
      and exports them separately &mdash; see
      <a href="#ec">section 6</a>. EC parts show an
      <b class="ui">EC</b> badge while grading.</li>
  </ul>
  <div class="note">Check that the Moodle assignment&rsquo;s
  <b class="ui">Maximum grade</b> equals the rubric&rsquo;s total
  <em>excluding</em> EC parts &mdash; the export warns you if they
  disagree.</div>

  <h2 id="server">3. Grading on the external server</h2>
  <ol>
    <li>In <b class="ui">External Grading</b> on the Grading tab, pick
      the assignment and click <b class="ui">Push to server</b>.</li>
    <li>The graders (and you) grade it at the grading site &mdash; the
      <b class="ui">Open grading site &#8599;</b> link. Progress shows
      live in the assignment&rsquo;s row here; everything anyone enters
      is labeled with their name.</li>
    <li>When grading is done, click <b class="ui">Pull grades</b> to copy
      their work into your local folder.</li>
    <li><b class="ui">Overview</b> (next to Pull, and in the
      grader&rsquo;s header) shows how the assignment went: average per
      part with the feedback pages&rsquo; score pies, the distribution of
      totals, who did particularly well or struggled, and whether the
      totals have been recorded in the course gradebook yet.</li>
  </ol>
  <div class="note warn">While an assignment is on the server, the server
  copy is the source of truth: don&rsquo;t also grade it locally, and
  <b>pull before you re-push</b> (a push mirrors your local copy over
  the server&rsquo;s, grades included &mdash; late submissions are fine
  to add this way, just pull first).</div>
  <p>Grading entirely on your own Mac works too, of course &mdash; skip
  the push/pull and grade under <b class="ui">Local Grading</b>.</p>

  <h2 id="export">4. Exporting</h2>
  <p>Open the assignment under <b class="ui">Local Grading</b> (after
  pulling) and click <b class="ui">Export</b> in the header &mdash; or run
  <code>hwgenie return &lt;folder&gt;</code>. Everything lands in the
  folder&rsquo;s <code>return/</code> directory:</p>
  <ul>
    <li><code>moodle-feedback.zip</code> &mdash; per-student feedback
      pages for Moodle.</li>
    <li><code>grading-worksheet-upload.csv</code> &mdash; your worksheet
      with the Grade column filled in (base points only, never above the
      assignment max, after any late penalty; students held for a
      late-work discussion are left blank).</li>
    <li><code>extra-credit-upload.csv</code> &mdash; only when the rubric
      has EC parts: each student&rsquo;s EC points, matched by email.</li>
    <li><code>gradebook.csv</code> &mdash; per-part scores for your own
      records.</li>
  </ul>

  <h2 id="moodle">5. Returning everything to Moodle</h2>
  <p>Back on <b class="ui">View all submissions</b>, from the
  <b class="ui">Grading action</b> dropdown:</p>
  <ol>
    <li><b class="ui">Upload multiple feedback files in a zip</b> &rarr;
      <code>moodle-feedback.zip</code>. Each student gets their feedback
      page; this does <em>not</em> set grades.</li>
    <li><b class="ui">Upload grading worksheet</b> &rarr;
      <code>grading-worksheet-upload.csv</code>. This is what enters the
      grades.</li>
  </ol>

  <h2 id="ec">6. Extra credit in the Moodle gradebook</h2>
  <p>Moodle&rsquo;s own mechanism for points above an assignment&rsquo;s
  maximum is a separate gradebook item flagged as extra credit. One-time
  setup per assignment that has EC:</p>
  <ol>
    <li><b class="ui">Gradebook setup</b> &rarr; <b class="ui">Add grade
      item</b>: name it e.g. &ldquo;PS3 extra credit&rdquo;, maximum =
      the EC points available, same category as the homework.</li>
    <li>Edit the item&rsquo;s settings in its category and tick
      <b class="ui">Extra credit</b> (available under Natural
      aggregation). Earned points then add to students&rsquo; totals
      without raising the denominator.</li>
  </ol>
  <p>Then each week: <b class="ui">Grades &rarr; Import &rarr; CSV
  file</b>, upload <code>extra-credit-upload.csv</code>, map
  <b class="ui">Email address</b> as the identifier, and map the
  <b class="ui">Extra credit</b> column onto the grade item.</p>

  <h2 id="late">7. Late work and the course gradebook</h2>
  <p>Moodle stamps every submission (&ldquo;Last modified
  (submission)&rdquo; in the worksheet, and the zip&rsquo;s file dates);
  <code>collect</code> records it per student. Give the assignment a
  deadline &mdash; <code>collect --due "2026-09-04 23:59"</code>, or a
  <code>due: 2026-09-04 23:59</code> line in <code>rubric.yml</code>
  (add <code>timezone: America/New_York</code> if the machine isn&rsquo;t
  in course time) &mdash; and hwGrader shows a red <b class="ui">late</b>
  badge with the delay on every late student, in the sidebar and in both
  views. Graders see the badge; the decision is yours.</p>
  <p><b>The policy</b> (edit <code>policy</code> in the course
  <code>gradebook.json</code> to change it): each student&rsquo;s first
  late assignment within 72 h is free. After that, up to 24 h late costs
  5% of the possible points, 24&ndash;72 h costs 10%, and anything later
  is <em>held</em> &mdash; the worksheet grade is left blank until you
  decide. Part scores are never touched: graders grade the work as
  submitted and the penalty is applied to the total at export, where the
  feedback page tells the student what happened
  (&ldquo;Submitted 2 h after the deadline. This used your free late
  assignment&hellip;&rdquo;).</p>
  <p><b>Intervening.</b> On your own (local) hwGrader, the late line
  under a student&rsquo;s name has a dropdown: <b class="ui">Follow the
  policy</b>, <b class="ui">Use the free late</b>, <b class="ui">Apply
  the penalty</b>, <b class="ui">Waive</b> (a two-minutes-late grace),
  <b class="ui">Extension to&hellip;</b> (tiers then count from the new
  deadline), or <b class="ui">Hold</b>. Add a note if you like and click
  <b class="ui">Save</b>. Decisions live in <code>late.json</code> in the
  grading folder &mdash; never pushed or pulled &mdash; so pulling grades
  cannot undo them.</p>
  <p><b>The gradebook.</b> Every export records each student&rsquo;s
  totals (after the policy), lateness and the action taken in
  <code>gradebook.json</code> one level above the assignment folders
  (e.g. <code>grading-lab/math221/</code>), with a <code>gradebook.csv</code>
  twin for spreadsheets. It also remembers which assignment consumed
  the free late, which is how the next export knows the student no
  longer has one. The <b class="ui">Gradebook</b> button in the header
  shows it as a table. Exporting again after changing a decision
  updates the record (and releases the free late if you switched a
  student off it).</p>

  <h2 id="trouble">Troubleshooting</h2>
  <ul>
    <li><b>External Grading says the server is unreachable</b> &mdash;
      check Tailscale is connected (menu-bar icon) on your Mac; then
      that the server is up (DigitalOcean dashboard).</li>
    <li><b>A grader can&rsquo;t load the site</b> &mdash; their Tailscale
      is off, or their share invite was never accepted.</li>
    <li><b>extra-credit-upload.csv has blank emails</b> &mdash; the
      grading worksheet wasn&rsquo;t in the grading folder at export;
      add it and export again.</li>
    <li><b>Worksheet rows skipped as locked</b> &mdash; those grades are
      locked in Moodle (already overridden there); unlock them in the
      gradebook or leave them.</li>
    <li><b>AI draft suggestions</b> &mdash; run the
      <code>/grade-review</code> skill in Claude Code on the grading
      folder <em>before</em> pushing to the server, and the graders see
      the drafts alongside each part.</li>
  </ul>
</main>
</body>
</html>
""".replace("__BASE__", BASE_CSS)
