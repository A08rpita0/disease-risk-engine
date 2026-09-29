/* Dashboard for the disease correlation & risk engine. */
(function () {
  "use strict";

  var state = { file: null, result: null, config: null };

  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pct(x) { return Math.round((x || 0) * 100) + "%"; }

  // A patient reads words, not a 0-1 score. The number stays available under
  // Technical details for anyone who wants it.
  // How closely results match a configured pattern - never how likely a disease is.
  var LEVEL_WORD = { High: "Strong match", Moderate: "Moderate match",
                     Low: "Weak match", Limited: "Not enough data" };

  /* The name the evidence supports, and - when it differs - the reference condition it
     relates to. Raised LDL is a "Cardiovascular risk signal"; the Disease Master row it is
     linked to is shown as a reference, never as the finding's name. */
  function riskName(r) { return r.display_name || r.name; }
  function riskReference(r) {
    if (!r.display_name || r.display_name === r.name) return "";
    return '<div class="risk-ref small muted">' + esc(r.display_note || "") +
      ' <span class="nowrap">Reference condition: ' + esc(r.name) + ".</span></div>";
  }
  /* One number style everywhere, independent of the browser's locale: at most four
     decimals with trailing zeros dropped, and digit grouping (Western 1,234,567) from
     10,000 up, where a count is always a whole number. */
  var NUMBER_LOCALE = "en-US";
  function num(v) {
    if (v === null || v === undefined) return "—";
    if (typeof v !== "number") return esc(v);
    if (Math.abs(v) >= 10000) {
      return v.toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: Math.abs(v) >= 100000 ? 0 : 2 });
    }
    return String(Math.round(v * 10000) / 10000);
  }

  /* A label adds nothing when it only repeats the value or the badge beside it:
     "Equivocal" printed as the value, the badge "Equivocal result" and the grade
     "Equivocal" said the same thing three times. */
  function words(s) { return String(s === null || s === undefined ? "" : s).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(); }
  function repeats(label, value, badge) {
    var l = words(label);
    if (!l) return true;
    var v = typeof value === "number" ? "" : words(value);
    return l === v || words(badge).indexOf(l) >= 0 || (v && l.indexOf(v) >= 0);
  }

  /* ---------------- upload wiring ---------------- */

  var dz = $("dropzone"), fi = $("fileInput");
  dz.addEventListener("click", function () { fi.click(); });
  dz.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fi.click(); }
  });
  ["dragenter", "dragover"].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove("over"); });
  });
  dz.addEventListener("drop", function (e) {
    if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
  });
  fi.addEventListener("change", function () { if (fi.files.length) setFile(fi.files[0]); });

  function setFile(f) {
    state.file = f;
    $("chosen").innerHTML = "Selected: <b>" + esc(f.name) + "</b> · " +
      (f.size > 1048576 ? (f.size / 1048576).toFixed(1) + " MB" : Math.ceil(f.size / 1024) + " KB");
    $("analyseBtn").disabled = false;
  }

  function status(msg, kind) {
    var el = $("status");
    el.hidden = false;
    el.className = "status " + (kind || "info");
    el.innerHTML = msg;
  }

  $("analyseBtn").addEventListener("click", function () {
    if (!state.file) return;
    var fd = new FormData();
    fd.append("file", state.file);
    if ($("sexInput").value) fd.append("sex", $("sexInput").value);
    if ($("ageInput").value) fd.append("age", $("ageInput").value);
    send("/api/analyse", fd);
  });

  function send(url, fd) {
    clearReject();
    $("analyseBtn").disabled = true;
    status('<span class="spin"></span>Reading your report and checking it against clinical guidelines…');
    fetch(url, { method: "POST", body: fd })
      .then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) {
            // Not a laboratory report: show what we looked for, not a raw error.
            if (body.document) { rejectDocument(body.document); return null; }
            throw new Error(body.detail || ("HTTP " + r.status));
          }
          return body;
        });
      })
      .then(function (data) { return data; })
      .then(function (data) {
        if (!data) return;                 // already handled as a rejected document
        state.result = data;
        var incomplete = data.document && data.document.incomplete;
        status((incomplete ? "<b>Analysis INCOMPLETE</b> — " + esc(incomplete.message) + " Read so far: "
                           : "Analysis complete — ") + data.summary.parameters_recognised +
               " parameters recognised, " + data.summary.conditions_flagged +
               " conditions flagged.",
               incomplete ? "error" : "info");
        render();
        $("results").hidden = false;
        collapseUpload(data);
        $("results").scrollIntoView({ behavior: "smooth", block: "start" });
      })
      .catch(function (e) { status("Could not analyse this file: " + esc(e.message), "error"); })
      .finally(function () { $("analyseBtn").disabled = !state.file; });
  }

  /* ---------------- re-run with a stated sex ---------------- */

  document.addEventListener("click", function (e) {
    var b = e.target.closest("#sexPrompt button[data-sex]");
    if (!b || !state.file) return;
    $("sexInput").value = b.dataset.sex;
    var fd = new FormData();
    fd.append("file", state.file);
    fd.append("sex", b.dataset.sex);
    if ($("ageInput").value) fd.append("age", $("ageInput").value);
    send("/api/analyse", fd);
  });

  /* ---------------- upload card collapse ---------------- */

  function collapseUpload(d) {
    $("uploadBody").hidden = true;
    $("reopenBtn").hidden = false;
    $("reopenBtn").setAttribute("aria-expanded", "false");
    var who = (d.patient && d.patient.name) ? d.patient.name : "this report";
    $("uploadTitle").textContent = "Analysed " + who;
  }

  function expandUpload() {
    $("uploadBody").hidden = false;
    $("reopenBtn").hidden = true;
    $("reopenBtn").setAttribute("aria-expanded", "true");
    $("uploadTitle").textContent = "Analyse a report";
  }

  $("reopenBtn").addEventListener("click", function () {
    expandUpload();
    $("uploadCard").scrollIntoView({ behavior: "smooth", block: "start" });
  });

  /* ---------------- rejected document ---------------- */

  function rejectDocument(doc) {
    state.result = null;
    $("results").hidden = true;            // no empty dashboard behind the message

    var s = doc.signals || {};
    var detail = doc.status === "unreadable"
      ? "No readable text was found in this file."
      : "Read " + (s.text_characters || 0).toLocaleString() + " characters, of which " +
        (s.parameters_recognised || 0) + " matched a known health parameter.";

    $("status").hidden = true;
    $("reject").hidden = false;
    $("reject").innerHTML =
      '<div class="rj-head"><span class="rj-icon">!</span><div>' +
        "<h2>" + esc(doc.title) + "</h2>" +
        '<p class="rj-sub">' + esc(doc.guidance) + "</p></div></div>" +
      '<div class="rj-body">' +
        '<div class="rj-what"><b>What we can read</b><ul>' +
        (doc.accepted_formats || []).map(function (f) {
          return "<li>" + esc(f) + "</li>";
        }).join("") + "</ul></div>" +
        '<div class="rj-sig"><b>What this file looked like</b><br>' + esc(detail) + "</div>" +
      "</div>";
    $("reject").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function clearReject() { $("reject").hidden = true; }

  /* ---------------- tabs (ARIA tablist, arrow-key navigable) ---------------- */

  function visibleTabs() {
    return [].slice.call(document.querySelectorAll(".tab")).filter(function (t) {
      return !t.hidden;
    });
  }

  function selectTab(btn, focus) {
    if (!btn || btn.hidden) return;
    document.querySelectorAll(".tab").forEach(function (t) {
      var on = t === btn;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
    });
    document.querySelectorAll(".panel").forEach(function (p) {
      p.classList.toggle("active", p.id === "tab-" + btn.dataset.tab);
    });
    if (focus) btn.focus();
  }

  $("tabs").addEventListener("click", function (e) {
    var b = e.target.closest(".tab");
    if (b) selectTab(b);
  });

  // Left/Right move between tabs, Home/End jump to the ends - the expected
  // keyboard behaviour for a tablist, and the only way to reach tabs without a mouse.
  $("tabs").addEventListener("keydown", function (e) {
    var keys = { ArrowLeft: -1, ArrowRight: 1, Home: "first", End: "last" };
    if (!(e.key in keys)) return;
    var tabs = visibleTabs(), i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    var move = keys[e.key];
    var next = move === "first" ? 0
             : move === "last" ? tabs.length - 1
             : (i + move + tabs.length) % tabs.length;
    selectTab(tabs[next], true);
  });

  /* ---------------- print ---------------- */

  $("printBtn").addEventListener("click", function () { window.print(); });

  /* ---------------- render ---------------- */

  function render() {
    var d = state.result;
    $("pillRisks").textContent = (d.abnormal_findings || []).length + d.disease_risks.length;
    var tabBtn = $("tabbtn-risks");
    if (tabBtn && (d.direct_findings || []).length) {
      tabBtn.title = (d.direct_findings.length) + " direct, " +
        ((d.pattern_findings || []).length) + " pattern";
    }
    $("pillParams").textContent = d.parameters.length;
    $("pillPlan").textContent = d.recommendations.length;

    renderOverview(d);
    renderRisks(d);
    renderParams(d);
    renderPlan(d);
  }

  function patientLine(p) {
    return p.name ? esc(p.name) : '<span class="muted">Name not stated in the report</span>';
  }

  function findingRows(list) {
    return list.map(function (f) {
      return '<div class="finding-row"><span class="badge b-' +
        (f.severity_score >= 0.75 ? "High" : f.severity_score >= 0.5 ? "Moderate" : "Low") + '">' +
        esc(f.grade_label || (f.direction === "low" ? "Low" : "High")) + "</span>" +
        "<b>" + esc(f.name) + "</b> " + '<span class="num">' + num(f.value) + "</span> " +
        '<span class="muted small">' + esc(f.unit || "") + "</span></div>";
    }).join("");
  }

  /* The analysis time, shown in the reader's own timezone. The server sends its offset,
     so a report analysed in India no longer prints the deployed server's UTC clock. */
  function analysedAt(iso) {
    var t = new Date(iso);
    if (isNaN(t)) return String(iso || "").replace("T", " ");
    return t.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit"
    });
  }

  function renderOverview(d) {
    var s = d.summary, out = "";

    // Printed copies need to say whose report this is; the page header is hidden on paper.
    $("printHeader").innerHTML =
      "<h2>Lab report analysis</h2><dl class=\"kv\">" +
      "<dt>Patient</dt><dd>" + patientLine(d.patient) + "</dd>" +
      "<dt>Report</dt><dd>" + esc(d.source_file) + "</dd>" +
      "<dt>Analysed on</dt><dd>" + esc(analysedAt(d.generated_at)) + "</dd></dl>" +
      '<p class="print-note">' + esc(d.disclaimer) + "</p>";

    // Part of the file could not be read: said first, before any result, so a partial
    // analysis is never mistaken for the whole report.
    if (d.document && d.document.incomplete) {
      out += '<div class="callout urgent" role="alert"><b>This analysis is incomplete.</b> ' +
        esc(d.document.incomplete.message) + "</div>";
    }

    out += '<div class="card"><h2>Record</h2><dl class="kv">' +
      "<dt>Patient</dt><dd>" + patientLine(d.patient) + "</dd>" +
      "<dt>Source file</dt><dd>" + esc(d.source_file) + "</dd>" +
      "<dt>Analysed</dt><dd>" + esc(analysedAt(d.generated_at)) + "</dd>" +
      "</dl></div>";

    // Sex changes several reference ranges, so say so and offer to re-run rather than
    // burying it in the action plan after the fact.
    if (!d.patient.sex) {
      out += '<div class="callout warn" id="sexPrompt"><b>Sex was not stated in this report.</b> ' +
        "Haemoglobin, ferritin, creatinine, HDL and uric acid are all read against different " +
        "ranges for men and women, so some results here may change. " +
        '<span class="sex-actions">Re-run as ' +
        '<button class="ghost sm" data-sex="male">Male</button>' +
        '<button class="ghost sm" data-sex="female">Female</button></span></div>';
    }

    var labOut = d.parameters.filter(function (p) {
      return p.finding_basis === "lab_range";
    }).length;
    out += '<div class="stats">' +
      stat(s.parameters_recognised, "results read") +
      stat(labOut, "outside lab range", labOut ? "alert" : "ok") +
      "</div>";

    if (s.parameters_unmapped) {
      out += '<div class="callout info">' + s.parameters_unmapped +
        " item" + (s.parameters_unmapped === 1 ? "" : "s") + " in your report " +
        (s.parameters_unmapped === 1 ? "was" : "were") + " not recognised as a test we " +
        "know, so " + (s.parameters_unmapped === 1 ? "it was" : "they were") +
        " left out of this analysis. Everything else was read normally.</div>";
    }

    if (d.urgent_findings.length) {
      out += '<div class="callout urgent"><b>Time-critical findings.</b> ' +
        d.urgent_findings.map(function (r) { return esc(riskName(r)); }).join(", ") +
        " — these need prompt medical assessment. " +
        "Seek medical care now rather than waiting for a routine appointment.</div>";
    }

    if ((d.abnormal_findings || []).length) {
      var af = d.abnormal_findings;
      out += '<div class="card"><h2>Results needing attention (' + af.length + ")</h2>" +
        findingRows(af.slice(0, 6)) +
        // The rest are behind a disclosure rather than a dead "and 3 more." line: the
        // count told the reader something was hidden without giving them any way to see
        // it. <details> also prints open, so a printed report carries every row.
        (af.length > 6
          ? "<details><summary>Show the other " + (af.length - 6) + " result" +
            (af.length - 6 === 1 ? "" : "s") + "</summary>" +
            findingRows(af.slice(6)) + "</details>"
          : "") + "</div>";
    }

    if (!(d.direct_findings || []).length && !(d.derived_findings || []).length &&
        !(d.pattern_findings || []).length) {
      out += '<div class="empty"><div class="big">✓</div>' +
        "<b>No disease risks were flagged from the parameters available.</b><br>" +
        (s.abnormal_count
          ? "Some parameters are outside their reference range but they do not form any of the clinically established clusters this engine detects."
          : "All recognised parameters fall within their reference ranges.") +
        ((d.insufficient_findings || []).length
          ? "<br><span class=\"small muted\">" + d.insufficient_findings.length +
            " condition" + (d.insufficient_findings.length === 1 ? " was" : "s were") +
            " considered and assessed but not supported by the evidence here — see " +
            "<b>What we found</b>.</span>"
          : "") +
        "</div>";
    } else {
      out += '<div class="card"><h2>What we found</h2>';
      // Only what the evidence actually supports is listed here. An insufficient-evidence
      // condition shown in the same five-row summary, at the same size, is exactly how a
      // "we looked and found nothing" reads as a finding.
      var headline = (d.direct_findings || []).concat(d.derived_findings || [])
        .concat(d.pattern_findings || []);
      headline.sort(function (a, b) { return b.score - a.score; });
      headline.slice(0, 5).forEach(function (r) {
        var isDirect = r.presentation_tier === "direct" || r.presentation_tier === "derived";
        out += '<div class="finding-row"><span class="badge b-' +
          (isDirect ? "direct" : r.evidence_level) + '">' +
          (r.presentation_tier === "derived" ? "Calculated finding"
            : r.presentation_tier === "direct" ? "Direct finding"
            : esc(LEVEL_WORD[r.evidence_level] || r.evidence_level)) + "</span>" +
          "<b>" + esc(riskName(r)) + "</b>" +
          '<div class="bar" role="img" aria-label="' +
            esc(LEVEL_WORD[r.evidence_level] || r.evidence_level) + '">' +
            '<i class="lv-' + r.evidence_level + '" style="width:' + pct(r.score) + '"></i></div>' +
          '<span class="w tech-only">' + r.score.toFixed(2) + "</span></div>";
      });
      out += '<p class="small muted" style="margin:12px 0 0">Open <b>What we found</b> for the ' +
        "evidence for and against each one, and <b>Action plan</b> for what to do.</p></div>";
    }

    if (d.coverage.capped_note) {
      out += '<div class="callout warn"><b>Limited evidence.</b> ' + esc(d.coverage.capped_note) +
        " Affected: " + d.coverage.capped_conditions.map(esc).join(", ") + ".</div>";
    }

    $("tab-overview").innerHTML = out;
  }

  function stat(n, label, cls) {
    return '<div class="stat ' + (cls || "") + '"><div class="n">' + n +
      '</div><div class="l">' + label + "</div></div>";
  }

  /* A measurement, rendered the same way wherever it appears so a reader can compare
     them: value, unit, the range it was judged against, and where that range came
     from. "Reference" alone hid the difference between the lab's own interval and a
     guideline band this engine applied when the lab supplied none. */
  function measure(m, cls) {
    var ref = "";
    if (m.reference_low !== null && m.reference_low !== undefined &&
        m.reference_high !== null && m.reference_high !== undefined) {
      ref = num(m.reference_low) + "–" + num(m.reference_high);
    } else if (m.reference_high !== null && m.reference_high !== undefined) {
      ref = "up to " + num(m.reference_high);
    } else if (m.reference_low !== null && m.reference_low !== undefined) {
      ref = num(m.reference_low) + " or above";
    }
    return '<div class="meas ' + (cls || "") + '">' +
      '<span class="meas-name">' + esc(m.name) + "</span>" +
      '<span class="meas-val">' + num(m.value) +
        (m.unit ? ' <span class="meas-unit">' + esc(m.unit) + "</span>" : "") + "</span>" +
      (ref ? '<span class="meas-ref">ref ' + esc(ref) + "</span>" : "") +
      '<span class="meas-read">' + esc(m.reading || "") + "</span></div>";
  }

  /* A direct finding is one measured value against a configured threshold. A pattern is
     a multi-marker hypothesis. Rendering them in one list lets a hypothesis read like a
     measured fact, so they get separate sections with different wording. */
  function directCard(r) {
    var e = r.direct_evidence || {};
    var ref = "";
    if (e.reference_low !== null && e.reference_low !== undefined &&
        e.reference_high !== null && e.reference_high !== undefined) {
      ref = num(e.reference_low) + " – " + num(e.reference_high);
    } else if (e.reference_high !== null && e.reference_high !== undefined) {
      ref = "up to " + num(e.reference_high);
    } else if (e.reference_low !== null && e.reference_low !== undefined) {
      ref = num(e.reference_low) + " or above";
    }
    var derived = r.presentation_tier === "derived";
    return '<div class="direct">' +
      '<div class="direct-head"><span class="badge b-direct">' +
        (derived ? "Calculated finding" : "Direct finding") + "</span>" +
      "<b>" + esc(riskName(r)) + "</b></div>" + riskReference(r) +
      '<div class="direct-measure"><span class="dm-param">' + esc(e.parameter || "") + "</span>" +
      '<span class="dm-value">' + num(e.value) +
        (e.unit ? ' <span class="dm-unit">' + esc(e.unit) + "</span>" : "") + "</span>" +
      (ref ? '<span class="dm-ref">' +
             // Only call it the laboratory's reference when the laboratory's interval
             // is what the verdict rests on. Vitamin D 14.2 against a report interval
             // of "up to 20" is called deficient by a configured guideline band.
             (e.reference_source && e.reference_source.indexOf("report") === 0 &&
              e.graded_by !== "decision_band"
               ? "Laboratory reference: " : "Guideline threshold: ") + esc(ref) +
             (e.unit ? " " + esc(e.unit) : "") + "</span>" : "") +
      (e.graded_by === "decision_band"
        ? '<span class="dm-ref">The interval printed on the report would not flag this; ' +
          "it is judged against the standard clinical band.</span>" : "") +
      "</div>" +
      (e.grade_label ? '<div class="direct-read">' + esc(e.grade_label) + "</div>" : "") +
      (e.statement ? '<p class="direct-note">' + esc(e.statement) + "</p>" : "") +
      (r.context_values && r.context_values.length
        ? '<div class="ctx-block"><div class="ctx-h">Related results, within range</div>' +
          r.context_values.slice(0, 4).map(function (m) { return measure(m, "ok"); }).join("") +
          "</div>"
        : "") +
      '<p class="direct-note muted">This is what the measurement itself shows. It is not a ' +
      "diagnosis — your doctor interprets it alongside your symptoms and history.</p>" +
      "</div>";
  }

  /* One pattern card. The title stays the name the clinical reference uses, but the
     framing above it says what the engine is actually claiming: that results resemble
     a pattern associated with this condition. Supporting, contradicting and missing
     evidence are all shown, because a card that lists only what fired reads like a
     case being made rather than a picture being described. */
  function patternCard(r, i, tier) {
    var out = '<div class="risk lv-' + r.evidence_level + " t-" + tier +
      '" data-i="' + i + '">';
    out += '<div class="risk-head"><div><div class="risk-title">' +
      '<span class="sig-kind">' +
        (tier === "insufficient" ? "Considered" : "Pattern") + "</span>" +
      esc(riskName(r)) + "</div>" + riskReference(r);
    out += '<div class="risk-meta">' +
      '<span class="badge b-' + r.evidence_level + '">' +
        esc(LEVEL_WORD[r.evidence_level] || r.evidence_level) + "</span>" +
      '<span class="badge b-' + r.urgency_tier + '">' + esc(r.urgency_tier) + "</span>" +
      (r.display_name && r.display_name !== r.name ? ""
        : '<span class="badge b-tag">' + esc(r.classification) + "</span>") +
      (r.icd10 ? '<span class="badge b-tag">ICD-10 ' + esc(r.icd10) + "</span>" : "") +
      (r.evidence_capped ? '<span class="badge b-Limited">capped — thin data</span>' : "") +
      '<span class="small muted">' + r.profiles.map(esc).join(" · ") + "</span>" +
      "</div></div>";
    out += '<div class="risk-score tech-only"><div class="v">' + r.score.toFixed(2) +
      '</div><div class="c">evidence</div></div></div>';

    out += '<div class="risk-body">';

    // The three-way evidence picture, above the machinery.
    // Discounted triggers are shown too. Redundancy capping stops two views of the
    // same biology inflating the SCORE; it is not a reason to hide the measurement.
    // ApoB 142 mg/dL was being left out of the evidence for a lipid pattern because
    // the ApoB/ApoA1 ratio carried the weight, which made the card look thinner than
    // the record actually is.
    var supporting = r.triggering_parameters;
    if (supporting.length) {
      out += '<div class="section"><h4>Results supporting this</h4>' +
        supporting.map(function (t) {
          return '<div class="meas hit' + (t.discounted ? " dim" : "") + '">' +
            '<span class="meas-name">' + esc(t.name) + "</span>" +
            '<span class="meas-val">' + esc(t.observed || "") + "</span>" +
            '<span class="meas-read">' + esc(t.finding || "") +
            (t.discounted ? " · counted at reduced weight, same biology as another "
                          + "result above" : "") + "</span></div>";
        }).join("") + "</div>";
    }
    if (r.contradicting && r.contradicting.length) {
      out += '<div class="section"><h4>Results arguing against this</h4>' +
        '<p class="small muted" style="margin:0 0 6px">These were measured and came back ' +
        "normal, so the evidence score above has already been reduced.</p>" +
        r.contradicting.map(function (m) { return measure(m, "against"); }).join("") + "</div>";
    }
    if (r.context_values && r.context_values.length) {
      out += '<div class="section"><h4>Related results, within range</h4>' +
        r.context_values.slice(0, 6).map(function (m) { return measure(m, "ok"); }).join("") +
        "</div>";
    }
    if (r.missing_parameters.length) {
      out += '<div class="section"><h4>Not measured</h4>' +
        '<p class="small muted" style="margin:0 0 6px">Missing, not normal — these were ' +
        "never tested, so they neither support nor rule this out.</p>" +
        '<div class="chips">' + r.missing_parameters.map(function (p) {
          return '<span class="chip miss">' + esc(paramName(p)) + "</span>";
        }).join("") + "</div>" +
        '<p class="small muted" style="margin:8px 0 0">Data coverage for this condition: <b>' +
        pct(r.data_coverage) + "</b> of its relevant markers.</p></div>";
    }

    out += '<div class="callout info small">' +
      (tier === "insufficient"
        ? "<b>Not established.</b> This was assessed and the evidence here does not support " +
          "reporting it as a finding. It is listed so you can see it was considered."
        : "<b>This is not a diagnosis.</b> It means some of your results resemble a pattern " +
          "that is associated with this condition. Establishing it needs a clinician, and " +
          "usually the confirmatory tests listed below.") + "</div>";

    out += '<div class="section"><h4>Why this was flagged</h4>' +
      '<div class="explain">' + esc(r.explanation) + "</div></div>";

    out += '<div class="section tech-only"><h4>Evidence contributions (combined with noisy-OR)</h4>';
    r.contributions.forEach(function (c) {
      out += '<div class="contrib"><span class="role-tag">' + esc(c.role) + "</span>" +
        "<span style=\"flex:0 0 auto\">" + esc(c.cohort_name) + "</span>" +
        '<div class="bar"><i style="width:' + pct(c.contribution) + '"></i></div>' +
        '<span class="w">' + c.contribution.toFixed(3) +
        '  (w ' + c.link_weight + " × conf " + c.cohort_confidence.toFixed(2) +
        (c.support_penalty !== undefined && c.support_penalty < 1
          ? " × support " + c.support_penalty.toFixed(2) : "") +
        ")</span></div>";
      if (c.support_note) out += '<div class="basis">Damped — ' + esc(c.support_note) + "</div>";
      if (c.dm_basis) out += '<div class="basis">Clinical basis — ' + esc(c.dm_basis) + "</div>";
    });
    var sb = r.score_breakdown || {};
    if (sb.band_from_score) {
      out += '<div class="basis">Score ' + (sb.score !== undefined ? sb.score.toFixed(3) : "") +
        " gives band " + esc(sb.band_from_score) + " → reported as " + esc(sb.final_level) +
        (sb.cap_applied ? " (" + esc(sb.cap_applied) + ")" : "") +
        (sb.direct_exempt_from_coverage_cap ? " (direct finding: not capped for untested markers)" : "") +
        ". Coverage " + pct(sb.coverage || 0) + ".</div>";
    }
    out += '<p class="small muted" style="margin:8px 0 0">These weights are engineering ' +
      "design values chosen so the rules behave consistently. They are not validated " +
      "clinical coefficients, and the score is a measure of evidence strength — it is " +
      "not a probability that you have this condition.</p></div>";

    out += "<details><summary>Full clinical details</summary><dl class=\"kv\">";
    ["Definition", "Common Symptoms", "Related Markers/Tests", "High-Risk Indicators",
     "Confirmatory/Diagnostic Tests", "Prognosis / Typical Course", "Possible Complications",
     "External Risk Factors", "Genetic/Family History Factors", "Other Important Factors",
     "Differential Diagnoses", "Prevention/Lifestyle Guidance", "Recommended Next Step",
     "Severity/Urgency Level", "Review Status", "Source / Reference"
    ].forEach(function (k) {
      if (r.dm_fields[k]) out += "<dt>" + esc(k) + "</dt><dd>" + esc(r.dm_fields[k]) + "</dd>";
    });
    out += "</dl></details>";

    if (r.conditional_urgency && r.conditional_urgency !== r.urgency_tier) {
      out += '<div class="callout warn"><b>Can become urgent.</b> Note that this ' +
        "condition can become <b>" + esc(r.conditional_urgency) + "</b>-level: " +
        esc(r.urgency_escalation || r.urgency_raw) + "</div>";
    }

    return out + "</div></div>";
  }

  var BASIS_LABEL = {
    lab_range: "Outside the laboratory's range",
    decision_threshold: "Guideline threshold",
    derived: "Calculated here",
    lab_flag: "Flagged by the laboratory",
    lab_expected_text: "Differs from the report's expected result",
    equivocal: "Equivocal result",
    weak_positive: "Weak positive (as reported)",
    trace: "Trace (as reported)",
    incomplete_screen: "Incomplete screen - not negative",
    conditional_range: "Depends on information not in the report",
    uninterpretable: "Could not be interpreted",
    conflicting_reading: "Reported more than once with different results",
    not_in_dictionary: "Not recognised here, marked by the report"
  };

  var NOTED_KINDS = { equivocal: 1, weak_positive: 1, trace: 1, incomplete_screen: 1,
                      conditional_range: 1, uninterpretable: 1, conflicting_reading: 1,
                      not_in_dictionary: 1 };

  /* Every abnormal result, whether or not any rule interprets it. A result used to
     reach this page only by feeding a Disease Master condition, so an hs-CRP of 31.98
     mg/L - which feeds none on its own - was flagged and then shown nowhere but the raw
     results table. The Disease Master enriches this list; it does not filter it. */
  function labFindingRow(f) {
    var badgeText = (f.kind === "qualitative" || f.kind === "categorical") && !NOTED_KINDS[f.finding_basis]
      ? "Reported by the laboratory" : (BASIS_LABEL[f.finding_basis] || f.finding_basis);
    var links = (f.linked || []).map(function (l) {
      var tier = l.kind === "pattern" ? "pattern"
        : (l.tier === "insufficient" ? "considered, not supported" : l.tier + " finding");
      return '<span class="chip">' + esc(l.name) + ' <span class="muted">' + esc(tier) +
        "</span></span>";
    }).join("");
    return '<div class="labf sev-' + (f.severity_score >= 0.75 ? "hi" : f.severity_score >= 0.5 ? "mid" : "lo") +
      (f.in_lab_range ? " in-range" : "") + '">' +
      '<div class="labf-top">' +
        '<span class="labf-name">' + esc(f.name) + "</span>" +
        '<span class="labf-val">' + num(f.value) +
          (f.unit ? ' <span class="meas-unit">' + esc(f.unit) + "</span>" : "") + "</span>" +
        (f.reference_text ? '<span class="labf-ref">ref ' + esc(f.reference_text) + "</span>" : "") +
        '<span class="badge b-tag">' + esc(badgeText) + "</span>" +
        (f.data_quality === "suspicious" ? '<span class="badge b-Limited" title="' +
          esc(f.data_quality_reason || "") + '">check this value</span>' : "") +
        (f.grade_label && !repeats(f.grade_label, f.value, badgeText)
          ? '<span class="labf-grade">' + esc(f.grade_label) + "</span>" : "") +
        (f.printed_band ? '<span class="labf-ref">printed band: ' + esc(f.printed_band) + "</span>" : "") +
        (f.lab_flag ? '<span class="labf-flag" title="Flag printed on the report">report flag: ' +
          esc(f.lab_flag) + "</span>" : "") +
      "</div>" +
      '<div class="labf-say">' + esc(f.statement) +
        (f.standalone ? " No condition or pattern in this analysis rests on this result on " +
          "its own, so none is suggested - it is listed so it is not missed." : "") + "</div>" +
      (links ? '<div class="labf-links"><span class="small muted">Also part of:</span> ' + links + "</div>" : "") +
      "</div>";
  }

  function labFindingsSection(d) {
    // Threshold results and results the laboratory marked are no longer listed here,
    // so this section shows - and is kept open by - the abnormal results alone.
    var abn = d.abnormal_findings || [];
    if (!abn.length) return "";
    var out = '<section class="tier"><h2 class="tier-h">Abnormal laboratory results' +
      '<span class="tier-n">' + abn.length + "</span></h2>";
    abn.forEach(function (f) { out += labFindingRow(f); });
    return out + "</section>";
  }

  function renderRisks(d) {
    var direct = d.direct_findings || [];
    var derived = d.derived_findings || [];
    var patterns = d.pattern_findings || [];
    if (!direct.length && !derived.length && !patterns.length &&
        !(d.abnormal_findings || []).length &&
        !(d.threshold_findings || []).length) {
      $("tab-risks").innerHTML = '<div class="empty"><div class="big">✓</div>' +
        "No result is outside its range and no condition reached the reporting threshold.</div>";
      return;
    }

    var out = labFindingsSection(d);

    if (direct.length) {
      out += '<section class="tier"><h2 class="tier-h">Direct findings' +
        '<span class="tier-n">' + direct.length + "</span></h2>" +
"";
      direct.forEach(function (r) { out += directCard(r); });
      out += "</section>";
    }

    if (derived.length) {
      out += '<section class="tier"><h2 class="tier-h">Calculated findings' +
        '<span class="tier-n">' + derived.length + "</span></h2>" +
"";
      derived.forEach(function (r) { out += directCard(r); });
      out += "</section>";
    }

    out += '<section class="tier"><h2 class="tier-h">Pattern / risk signals' +
      '<span class="tier-n">' + patterns.length + "</span></h2>" +
"";
    if (!patterns.length) {
      out += '<div class="empty small">No multi-marker patterns reached the reporting threshold.</div>';
    }
    patterns.forEach(function (r, i) { out += patternCard(r, i, "pattern"); });
    out += "</section>";

    $("tab-risks").innerHTML = out;

    $("tab-risks").addEventListener("click", function (e) {
      var h = e.target.closest(".risk-head");
      if (h) h.parentElement.classList.toggle("open");
    });
  }

  function paramName(id) {
    if (state.config) {
      for (var i = 0; i < state.config.parameters.length; i++) {
        if (state.config.parameters[i].id === id) return state.config.parameters[i].name;
      }
    }
    return id.replace(/_/g, " ");
  }


  /* Three different claims were sharing one "Outside the normal range" heading:
     the lab's own interval was breached, a guideline band this engine applies decided
     it because the lab supplied no interval, and the value was calculated here and
     never measured at all. A fourth case had nowhere to sit: TSH 5.05 inside a
     0.54-5.3 lab range still drives the hypothyroid pattern, so a reader saw the
     advice with no visible result behind it. */
  var BASIS_GROUPS = [
    { key: "lab_range", title: "Outside the laboratory reference range", cls: "g-lab" },
    { key: "decision_threshold", title: "Clinical decision threshold triggered", cls: "g-band" }
  ];

  function renderParams(d) {
    var out = "";
    var shown = {};

    // Values this engine calculated are still computed, and still drive pattern
    // detection; they are simply not listed on this page. They are marked as shown so
    // they do not fall through into "Within range" instead.
    d.parameters.forEach(function (p) {
      if (p.derived || p.finding_basis === "derived") shown[p.parameter_id] = 1;
    });
    var listed = 0;

    BASIS_GROUPS.forEach(function (g) {
      var rows = d.parameters.filter(function (p) { return p.finding_basis === g.key; });
      rows.forEach(function (p) { shown[p.parameter_id] = 1; });
      if (!rows.length) return;
      listed += rows.length;
      out += '<div class="card ' + g.cls + '"><h2>' + esc(g.title) + " (" + rows.length + ")</h2>" +
        paramTable(rows) + "</div>";
    });

    if (!listed) {
      out += '<div class="card"><h2>Outside the laboratory reference range (0)</h2>' +
        '<p class="muted small">None — every recognised result is within its range.</p></div>';
    }

    var norm = d.parameters.filter(function (p) { return !shown[p.parameter_id]; });
    out += '<div class="card"><h2>Within range (' + norm.length + ")</h2>" +
      (norm.length ? paramTable(norm) : '<p class="muted small">None.</p>') + "</div>";
    $("tab-params").innerHTML = out;
  }

  function paramTable(rows) {
    var out = '<div class="wrap"><table class="tbl"><thead><tr>' +
      "<th>Parameter</th><th>Profile</th><th>Result</th><th>Laboratory reference</th>" +
      "<th>Status</th></tr></thead><tbody>";
    rows.forEach(function (p) {
      var printed = p.raw && p.raw.raw_value !== undefined ? p.raw.raw_value : p.status;
      var value = p.kind === "qualitative"
        ? '<span class="badge b-' + (p.status || "normal") + '">' + esc(printed) + "</span>"
        : '<span class="num">' + num(p.value) + "</span> " +
          '<span class="muted small">' + esc(p.unit || "") + "</span>";
      if (p.data_quality === "suspicious") {
        value += ' <span class="badge b-Limited" title="' + esc(p.data_quality_reason || "") +
          '">check this value</span>';
      }
      var ref = "—";
      if (p.reference_low !== null || p.reference_high !== null) {
        ref = (p.reference_low !== null ? num(p.reference_low) : "") +
              (p.reference_low !== null && p.reference_high !== null ? " – " : "") +
              (p.reference_high !== null ? num(p.reference_high) : "");
        ref = '<span class="num">' + ref + "</span>";
      }

      // data-label drives the stacked card layout on phones, where the header row is hidden.
      out += '<tr class="' + (p.abnormal ? "abn" : "") + '">' +
        '<td data-label="Test"><b>' + esc(p.name) + "</b>" +
          (p.derived ? ' <span class="badge b-tag">derived</span>' : "") + "</td>" +
        '<td data-label="Profile" class="small muted">' + esc(p.profile || "—") + "</td>" +
        '<td data-label="Result">' + value + "</td>" +
        '<td data-label="Laboratory reference">' + ref + "</td>" +
        '<td data-label="Status">' + (p.abnormal
          ? '<span class="badge b-' + (p.direction || "high") + '">' + esc(p.direction || "") + "</span> "
          : "") + (repeats(p.grade_label || p.grade, printed, "") && p.kind === "qualitative"
            ? "" : '<span class="small">' + esc(p.grade_label || p.grade) + "</span>") +
          "</td></tr>";
    });
    return out + "</tbody></table></div>";
  }

  /* The plan is grouped by the FINDING it is about, with the measured result beside
     it. Grouping by action category scattered five separate vitamin D steps across
     Consultation, Diet, Lifestyle and Testing, and not one of them quoted the 14.2
     that prompted them. */
  var CAT_LABEL = {
    Urgent: "Urgent", Consultation: "Talk to your doctor", Testing: "Tests",
    Monitoring: "Monitor", Diet: "Food", Activity: "Activity", Lifestyle: "Daily habits"
  };
  var CAT_ORDER = ["Urgent", "Consultation", "Testing", "Monitoring", "Diet",
                   "Activity", "Lifestyle"];

  function valueChip(v) {
    var ref = "";
    if (v.reference_low !== null && v.reference_low !== undefined &&
        v.reference_high !== null && v.reference_high !== undefined) {
      ref = num(v.reference_low) + "–" + num(v.reference_high);
    } else if (v.reference_high !== null && v.reference_high !== undefined) {
      ref = "up to " + num(v.reference_high);
    }
    /* A value can be inside the lab's own range and still be what fired the finding
       (TSH 5.05 in a 0.54-5.3 range sits in the 4-10 subclinical band). Showing it in
       the same alarm colour as a genuinely out-of-range result would be misleading,
       so it is kept neutral and the band it fell into is spelled out instead. */
    return '<span class="vchip' + (v.in_range ? " in-range" : "") + '"><b>' +
      esc(v.name) + "</b> " +
      '<span class="vnum">' + (typeof v.value === "number" && Math.abs(v.value) >= 1
        ? num(Math.round(v.value * 100) / 100) : num(v.value)) + "</span>" +
      (v.unit ? " " + esc(v.unit) : "") +
      (ref ? ' <span class="vref">ref ' + esc(ref) + "</span>" : "") +
      (v.reading ? ' <span class="vread">' + esc(v.reading) + "</span>" : "") +
      "</span>";
  }

  function stepItem(r, n) {
    return '<li class="step p-' + r.priority + '">' +
      '<div class="step-top"><span class="step-cat">' +
        esc(CAT_LABEL[r.category] || r.category) + "</span>" +
      (r.timeframe ? '<span class="step-when">' + esc(r.timeframe) + "</span>" : "") +
      (r.priority === "urgent"
        ? '<span class="step-flag">Urgent</span>' : "") +
      "</div>" +
      '<div class="step-text">' + esc(r.text) + "</div></li>";
  }

  function renderPlan(d) {
    var recs = d.recommendations || [];
    if (!recs.length) {
      $("tab-plan").innerHTML = '<div class="empty">No recommendations.</div>';
      return;
    }

    var rank = { urgent: 0, high: 1, medium: 2, low: 3 };

    // --- group by finding, keeping the strongest finding first ---
    var groups = {}, order = [];
    recs.forEach(function (r) {
      var k = r.finding || "General";
      if (!groups[k]) {
        groups[k] = { name: r.finding_display || k, kind: r.finding_kind, items: [], values: [] };
        order.push(k);
      }
      groups[k].items.push(r);
      (r.values || []).forEach(function (v) {
        if (!groups[k].values.some(function (x) { return x.name === v.name; })) {
          groups[k].values.push(v);
        }
      });
    });
    order.sort(function (a, b) {
      var ga = groups[a], gb = groups[b];
      if ((a === "General") !== (b === "General")) return a === "General" ? 1 : -1;
      return Math.min.apply(null, ga.items.map(function (r) { return rank[r.priority]; })) -
             Math.min.apply(null, gb.items.map(function (r) { return rank[r.priority]; }));
    });

    var urgent = recs.filter(function (r) {
      return r.priority === "urgent" || r.priority === "high";
    });
    var scheduled = recs.filter(function (r) { return r.timeframe; });

    var out = '<div class="plan-intro"><b>Your action plan.</b> ' + recs.length +
      " steps across " + order.length + " finding" + (order.length === 1 ? "" : "s") +
      (urgent.length ? ", <b>" + urgent.length + "</b> worth raising sooner rather than later" : "") +
      ". Nothing here is a prescription — it is what to discuss and check with your doctor.</div>";

    // --- start here ---
    if (urgent.length) {
      out += '<section class="plan-group"><h3 class="plan-h">Start here' +
        '<span class="plan-n">' + Math.min(3, urgent.length) + "</span></h3>" +
        '<p class="plan-lead">If you do nothing else, do these.</p><ol class="plan-list">';
      urgent.slice(0, 3).forEach(function (r) {
        out += '<li class="step p-' + r.priority + '"><div class="step-top">' +
          '<span class="step-cat">' + esc(r.finding_display || r.finding) + "</span></div>" +
          '<div class="step-text">' + esc(r.text) + "</div></li>";
      });
      out += "</ol></section>";
    }

    // --- by finding ---
    order.forEach(function (k) {
      var g = groups[k];
      g.items.sort(function (a, b) {
        var d1 = rank[a.priority] - rank[b.priority];
        return d1 || (CAT_ORDER.indexOf(a.category) - CAT_ORDER.indexOf(b.category));
      });
      out += '<section class="plan-group finding-group">' +
        '<h3 class="plan-h">' + esc(g.name) +
        '<span class="plan-n">' + g.items.length + "</span></h3>";
      if (g.values.length) {
        out += '<div class="plan-values">' + g.values.map(valueChip).join("") + "</div>";
      }
      out += '<ol class="plan-list">';
      g.items.forEach(function (r, i) { out += stepItem(r, i); });
      out += "</ol></section>";
    });

    // --- follow-up schedule ---
    if (scheduled.length) {
      out += '<section class="plan-group"><h3 class="plan-h">Follow-up schedule' +
        '<span class="plan-n">' + scheduled.length + "</span></h3>" +
        '<p class="plan-lead">Re-tests worth booking, with the timing suggested above.</p>' +
        '<div class="sched">';
      scheduled.forEach(function (r) {
        out += '<div class="sched-row"><span class="sched-when">' + esc(r.timeframe) +
          "</span><span>" + esc(r.finding) + " — " + esc(r.text) + "</span></div>";
      });
      out += "</div></section>";
    }

    $("tab-plan").innerHTML = out;
  }

  /* "312 observations found, 70 recognised, 241 unmapped" read as a 77% failure rate.
     Almost all of those 241 were document fields - LabNo, SampleCollDate,
     ApprovedByDoctorID - that were never test results. The breakdown below separates
     what was actually dropped from what was never a result in the first place. */
  function dqRow(n, label, note, cls) {
    return '<div class="dq ' + (cls || "") + '"><div class="dq-n">' + n + "</div>" +
      '<div><div class="dq-l">' + esc(label) + "</div>" +
      (note ? '<div class="dq-note">' + esc(note) + "</div>" : "") + "</div></div>";
  }



  /* ---------------- boot ---------------- */

  fetch("/api/config/summary").then(function (r) { return r.json(); }).then(function (c) {
    state.config = c;
    $("footConfig").textContent = "";
  }).catch(function () {});
})();
