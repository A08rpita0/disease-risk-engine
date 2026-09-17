/* Clinical Analytics front end.
   Consumes the same API as before; only presentation changed. */
(function () {
  "use strict";

  var state = { file: null, result: null, config: null };
  var $ = function (id) { return document.getElementById(id); };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pct(x, d) { return (100 * (x || 0)).toFixed(d == null ? 0 : d) + "%"; }
  /* Report-style precision: decimals scale with magnitude, but never drop a digit that
     carries the clinical signal - urine specific gravity 1.005 must not become 1. */
  function num(v) {
    if (v === null || v === undefined || v === "") return "—";
    if (typeof v !== "number") return esc(v);
    var a = Math.abs(v);
    if (a >= 10000) return v.toLocaleString();
    var dp = a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 3;
    while (dp < 4 && a > 0 && Math.abs(parseFloat(v.toFixed(dp)) - v) / a > 0.001) dp++;
    // Strip trailing zeros only AFTER a decimal point - 400 must not become 4.
    return v.toFixed(dp).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  }
  function paramName(id) {
    var list = state.config && state.config.parameters;
    if (list) for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i].name;
    return String(id).replace(/_/g, " ");
  }

  var LEVEL_CHIP = { High: "chip-crit", Moderate: "chip-warn", Low: "chip-ok", Limited: "chip-mute" };
  var URGENCY_CHIP = { emergency: "chip-crit", specialist: "chip-warn",
                       monitoring: "chip-info", routine: "chip-mute" };

  /* ------------------------------------------------ intake */

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
    $("chosen").innerHTML = "Selected <b>" + esc(f.name) + "</b> · " +
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

  $("collapseBtn").addEventListener("click", function () {
    var body = $("intakeBody");
    body.hidden = !body.hidden;
    this.textContent = body.hidden ? "Change input" : "Hide";
  });

  function runSample(fileName, label) {
    var fd = new FormData();
    fd.append("file", fileName);
    if ($("sexInput").value) fd.append("sex", $("sexInput").value);
    $("chosen").innerHTML = "Sample <b>" + esc(label) + "</b>";
    send("/api/analyse/sample", fd);
  }

  function send(url, fd) {
    $("analyseBtn").disabled = true;
    status('<span class="spin"></span>Extracting parameters, detecting clusters and scoring against the Disease Master…');
    fetch(url, { method: "POST", body: fd })
      .then(function (r) {
        return r.json().then(function (b) {
          if (!r.ok) {
            var doc = b.document;
            throw new Error(doc ? doc.title + " " + (doc.guidance || "") : (b.detail || "HTTP " + r.status));
          }
          return b;
        });
      })
      .then(function (data) {
        state.result = data;
        var s = data.summary;
        var incomplete = data.document && data.document.incomplete;
        status((incomplete ? "<b>Analysis INCOMPLETE</b> — " + esc(incomplete.message) + " Read so far: <b>"
                           : "Analysis complete — <b>") + s.parameters_recognised + "</b> parameters, <b>" +
               s.cohorts_detected + "</b> clusters, <b>" + s.conditions_flagged +
               "</b> conditions flagged.", incomplete ? "error" : "info");
        render();
        $("results").hidden = false;
        $("collapseBtn").hidden = false;
        $("intakeBody").hidden = true;
        $("collapseBtn").textContent = "Change input";
        document.querySelectorAll(".nav-item").forEach(function (b) { b.disabled = false; });
        selectTab("overview");
        window.scrollTo({ top: 0, behavior: "smooth" });
      })
      .catch(function (e) { status("Could not analyse this file: " + esc(e.message), "error"); })
      .finally(function () { $("analyseBtn").disabled = !state.file; });
  }

  /* ------------------------------------------------ navigation */

  document.querySelector(".sidebar").addEventListener("click", function (e) {
    var b = e.target.closest(".nav-item");
    if (b && !b.disabled) selectTab(b.dataset.tab);
  });

  function selectTab(name) {
    document.querySelectorAll(".nav-item").forEach(function (t) {
      t.classList.toggle("active", t.dataset.tab === name);
    });
    document.querySelectorAll(".panel").forEach(function (p) {
      p.classList.toggle("active", p.id === "tab-" + name);
    });
  }

  /* ------------------------------------------------ render */

  function render() {
    var d = state.result;
    $("cRisks").textContent = d.disease_risks.length;
    $("cCohorts").textContent = d.cohorts.length;
    $("cParams").textContent = d.parameters.length;
    $("cPlan").textContent = d.recommendations.length;

    renderPatientBar(d);
    renderOverview(d);
    renderRisks(d);
    renderCohorts(d);
    renderParams(d);
    renderPlan(d);
    renderData(d);
    renderReference();
  }

  function field(label, value, cls) {
    return '<div class="pb-field"><div class="pb-label">' + esc(label) + "</div>" +
      '<div class="pb-value ' + (cls || "") + '">' + (value || '<span class="muted">—</span>') +
      "</div></div>";
  }

  function renderPatientBar(d) {
    var p = d.patient, s = d.summary;
    var out = "";
    out += field("Patient", p.name ? esc(p.name) : null);
    out += field("Identifier", p.patient_id ? esc(p.patient_id) : null, "id");
    out += field("Sex", p.sex ? esc(p.sex.charAt(0).toUpperCase() + p.sex.slice(1)) : null);
    out += field("Age", p.age ? Math.round(p.age) + " yrs" : null);
    out += field("Report date", p.report_date ? esc(p.report_date) : null);
    out += field("Source", esc(d.source_file), "muted");
    out += '<div class="pb-spacer"></div>';
    out += field("Analysed", esc(d.generated_at.replace("T", " ")), "muted");
    $("patientBar").innerHTML = out;
  }

  function metric(v, k, cls) {
    return '<div class="metric ' + (cls || "") + '"><div class="v">' + v +
      '</div><div class="k">' + k + "</div></div>";
  }

  function renderOverview(d) {
    var s = d.summary, out = "";

    if (d.document && d.document.incomplete) {
      out += '<div class="callout crit" role="alert" style="margin:0 0 14px"><b>This analysis is incomplete.</b> ' +
        esc(d.document.incomplete.message) + "</div>";
    }

    out += '<div class="metrics">' +
      metric(s.parameters_recognised, "Parameters") +
      metric(s.abnormal_count, "Abnormal", s.abnormal_count ? "crit" : "ok") +
      metric(s.cohorts_detected, "Clusters") +
      metric(s.high_evidence, "High evidence", s.high_evidence ? "crit" : "") +
      metric(s.moderate_evidence, "Moderate", s.moderate_evidence ? "warn" : "") +
      metric(s.limited_evidence, "Low / limited") +
      "</div>";

    if ((d.lab_noted_findings || []).length) {
      out += '<div class="callout warn" style="margin:0 0 14px"><b>Marked by the laboratory, not graded abnormal here.</b><ul style="margin:6px 0 0">' +
        d.lab_noted_findings.map(function (f) { return "<li>" + esc(f.statement) + "</li>"; }).join("") +
        "</ul></div>";
    }

    if (d.urgent_findings.length) {
      out += '<div class="callout crit" style="margin:0 0 14px"><b>Time-critical.</b> ' +
        d.urgent_findings.map(function (r) { return esc(r.name); }).join(", ") +
        " — the Disease Master marks these as needing immediate assessment.</div>";
    }

    out += '<div class="card"><div class="card-head">' +
      '<span class="card-title">Analysis coverage</span>' +
      '<span class="card-note">' + esc(s.analysis_confidence) + "</span></div>" +
      '<div class="card-body"><div class="prose">' + esc(d.coverage.note) + "</div>" +
      (d.coverage.capped_note
        ? '<div class="callout warn"><b>Reduced evidence level.</b> ' + esc(d.coverage.capped_note) +
          " Affected: " + d.coverage.capped_conditions.map(esc).join(", ") + ".</div>"
        : "") +
      "</div></div>";

    if (!d.disease_risks.length) {
      out += '<div class="empty"><div class="lead">No disease risks flagged</div>' +
        (s.abnormal_count
          ? "Some parameters fall outside their reference range, but they do not form any of the clinically established clusters this engine detects."
          : "All recognised parameters fall within their reference ranges.") + "</div>";
    } else {
      out += '<div class="card"><div class="card-head">' +
        '<span class="card-title">Findings summary</span>' +
        '<span class="card-note">' + d.disease_risks.length + " conditions</span></div>" +
        '<div class="card-body flush"><div class="tbl-wrap"><table class="tbl"><thead><tr>' +
        "<th>Condition</th><th>Evidence</th><th>Triage</th><th>Profiles</th>" +
        '<th class="r">Score</th><th class="r">Coverage</th></tr></thead><tbody>';
      d.disease_risks.forEach(function (r) {
        out += "<tr><td><span class=\"name\">" + esc(r.name) + "</span>" +
          (r.evidence_capped ? ' <span class="chip chip-out">capped</span>' : "") + "</td>" +
          '<td><span class="chip ' + (LEVEL_CHIP[r.evidence_level] || "chip-mute") + '">' +
            r.evidence_level + "</span></td>" +
          '<td><span class="chip ' + (URGENCY_CHIP[r.urgency_tier] || "chip-mute") + '">' +
            esc(r.urgency_tier) + "</span></td>" +
          '<td class="small muted">' + r.profiles.map(esc).join(", ") + "</td>" +
          '<td class="r num">' + r.score.toFixed(2) + "</td>" +
          '<td class="r num muted">' + pct(r.data_coverage) + "</td></tr>";
      });
      out += "</tbody></table></div></div></div>";
    }

    out += '<div class="callout info">' + esc(d.disclaimer) + "</div>";
    $("tab-overview").innerHTML = out;
  }

  function renderRisks(d) {
    if (!d.disease_risks.length) {
      $("tab-risks").innerHTML = '<div class="empty"><div class="lead">No conditions reached the reporting threshold</div>' +
        "Nothing to review.</div>";
      return;
    }
    var out = '<div class="card"><div class="card-head">' +
      '<span class="card-title">Detected conditions</span>' +
      '<span class="card-note">Select a row for the reasoning, contributions and missing tests</span>' +
      '</div><div class="card-body flush">';

    d.disease_risks.forEach(function (r, i) {
      out += '<div class="finding lv-' + r.evidence_level + '" data-i="' + i + '">';
      out += '<div class="finding-head"><div class="lv-bar"></div><div class="finding-main">';
      out += '<div class="finding-name">' + esc(r.name) + "</div>";
      out += '<div class="finding-tags">' +
        '<span class="chip ' + (LEVEL_CHIP[r.evidence_level] || "chip-mute") + '">' +
          r.evidence_level + " evidence</span>" +
        '<span class="chip ' + (URGENCY_CHIP[r.urgency_tier] || "chip-mute") + '">' +
          esc(r.urgency_tier) + "</span>" +
        '<span class="chip chip-out">' + esc(r.classification) + "</span>" +
        (r.icd10 ? '<span class="chip chip-out">ICD-10 ' + esc(r.icd10) + "</span>" : "") +
        (r.evidence_capped ? '<span class="chip chip-mute">evidence capped</span>' : "") +
        '<span class="small muted">' + r.profiles.map(esc).join(" · ") + "</span></div>";
      out += '</div><div class="finding-score"><div class="v">' + r.score.toFixed(2) +
        '</div><div class="k">score</div></div><div class="finding-toggle">▸</div></div>';

      out += '<div class="finding-body">';

      out += '<div class="fb-section"><div class="section-label">Why this was flagged</div>' +
        '<div class="prose">' + esc(r.explanation) + "</div></div>";

      out += '<div class="fb-section"><div class="section-label">Evidence contributions ' +
        "(combined with noisy-OR)</div>";
      r.contributions.forEach(function (c) {
        out += '<div class="contrib"><span class="role">' + esc(c.role) + "</span>" +
          "<span>" + esc(c.cohort_name) + "</span>" +
          '<div class="bar' + (c.contribution > 0.5 ? " hi" : "") + '"><i style="width:' +
            pct(c.contribution) + '"></i></div>' +
          '<span class="w">' + c.contribution.toFixed(3) + "</span></div>";
        if (c.dm_basis) {
          out += '<div class="quote">Disease Master basis — ' + esc(c.dm_basis) + "</div>";
        }
      });
      out += "</div>";

      var trig = r.triggering_parameters.filter(function (t) { return !t.discounted; });
      if (trig.length) {
        out += '<div class="fb-section"><div class="section-label">Triggering parameters</div>' +
          '<div class="chips">' + trig.map(function (t) {
            return '<span class="chip chip-crit" title="' + esc(t.observed || "") +
              " — via " + esc(t.via_cohort) + '">' + esc(t.name) + "</span>";
          }).join("") + "</div></div>";
      }

      if (r.missing_parameters.length) {
        out += '<div class="fb-section"><div class="section-label">Not measured — would raise confidence</div>' +
          '<div class="chips">' + r.missing_parameters.map(function (p) {
            return '<span class="chip chip-dash">' + esc(paramName(p)) + "</span>";
          }).join("") + "</div>" +
          '<div class="small muted" style="margin-top:6px">Marker coverage for this condition: ' +
          "<b>" + pct(r.data_coverage) + "</b></div></div>";
      }

      if (r.conditional_urgency && r.conditional_urgency !== r.urgency_tier) {
        out += '<div class="callout warn"><b>Can escalate.</b> The Disease Master notes this can ' +
          "become <b>" + esc(r.conditional_urgency) + "</b>-level: " +
          esc(r.urgency_escalation || r.urgency_raw) + "</div>";
      }

      out += "<details><summary>Disease Master record</summary><dl class=\"kv\">";
      ["Definition", "Common Symptoms", "Related Markers/Tests", "High-Risk Indicators",
       "Confirmatory/Diagnostic Tests", "Prognosis / Typical Course", "Possible Complications",
       "External Risk Factors", "Genetic/Family History Factors", "Other Important Factors",
       "Differential Diagnoses", "Prevention/Lifestyle Guidance", "Recommended Next Step",
       "Severity/Urgency Level", "Review Status", "Source / Reference"
      ].forEach(function (k) {
        if (r.dm_fields[k]) out += "<dt>" + esc(k) + "</dt><dd>" + esc(r.dm_fields[k]) + "</dd>";
      });
      out += "</dl></details></div></div>";
    });

    $("tab-risks").innerHTML = out + "</div></div>";
    $("tab-risks").addEventListener("click", function (e) {
      var h = e.target.closest(".finding-head");
      if (!h) return;
      var f = h.parentElement;
      f.classList.toggle("open");
      h.querySelector(".finding-toggle").textContent = f.classList.contains("open") ? "▾" : "▸";
    });
  }

  function renderCohorts(d) {
    if (!d.cohorts.length) {
      $("tab-cohorts").innerHTML = '<div class="empty"><div class="lead">No clusters detected</div>' +
        "No clinically established combination was present in these parameters.</div>";
      return;
    }
    var out = "";
    d.cohorts.forEach(function (c) {
      out += '<div class="card"><div class="card-head"><div class="cluster-head" style="flex:1">' +
        "<div><span class=\"card-title\">" + esc(c.name) + "</span>" +
        '<div class="finding-tags">' +
          '<span class="chip chip-out">' + esc(c.category) + "</span>" +
          (c.cross_profile_rationale ? '<span class="chip chip-info">cross-profile</span>' : "") +
          (c.mode === "count_of" ? '<span class="chip chip-out">counting rule</span>' : "") +
          (c.urgency_override ? '<span class="chip ' + (URGENCY_CHIP[c.urgency_override] || "chip-mute") +
            '">' + esc(c.urgency_override) + "</span>" : "") +
          '<span class="small muted">' + c.profiles_touched.map(esc).join(" · ") + "</span>" +
        "</div></div>" +
        '<div class="conf"><div class="v">' + pct(c.confidence) + '</div><div class="k">confidence</div></div>' +
        "</div></div><div class=\"card-body\">";

      out += '<div class="prose">' + esc(c.description) + "</div>";

      if (c.mode === "count_of") {
        out += '<div class="fb-section"><div class="section-label">Components met — ' +
          c.components_met.length + " of " + (c.components_met.length + c.components_unmet.length) +
          "</div>";
        c.components_met.forEach(function (m) {
          out += '<div class="signal"><span class="role">met</span><span>' + esc(m.label) +
            ' <span class="muted">— ' + esc(m.evidence) + "</span></span><span></span></div>";
        });
        c.components_unmet.forEach(function (m) {
          out += '<div class="signal dim"><span class="role">' +
            (m.status.indexOf("not assessable") === 0 ? "no data" : "not met") + "</span><span>" +
            esc(m.label) + ' <span class="muted">— ' + esc(m.status) + "</span></span><span></span></div>";
        });
        out += "</div>";
      }

      var hits = c.hits.filter(function (h) { return h.effective_weight > 0; });
      if (hits.length) {
        out += '<div class="fb-section"><div class="section-label">Signals</div>';
        hits.forEach(function (h) {
          out += '<div class="signal' + (h.suppressed_by ? " dim" : "") + '">' +
            '<span class="role">' + esc(h.role) + "</span><span>" +
            esc(h.label || h.parameter_name) + ' <span class="muted">— ' + esc(h.observed) + "</span>" +
            (h.suppressed_by ? '<div class="small muted">weight reduced to avoid double-counting with ' +
              esc(h.suppressed_by) + "</div>" : "") +
            '</span><span class="w">' + h.effective_weight.toFixed(2) + "</span></div>";
        });
        out += "</div>";
      }

      if (c.cross_profile_rationale) {
        out += '<div class="fb-section"><div class="section-label">Why this crosses profiles</div>' +
          '<div class="prose">' + esc(c.cross_profile_rationale) + "</div></div>";
      }

      out += '<div class="fb-section"><div class="section-label">Maps to</div>' +
        '<div class="chips">' + clusterDiseaseChips(c.cohort_id) + "</div></div>";

      if (c.evidence && c.evidence.length) {
        out += "<details><summary>Clinical references (" + c.evidence.length + ")</summary>";
        c.evidence.forEach(function (e) {
          out += '<div class="cite"><b>' + esc(e.citation) + "</b><br>" + esc(e.note) + "</div>";
        });
        out += "</details>";
      }
      if (c.parameters_missing.length) {
        out += '<div class="small muted" style="margin-top:9px">Data coverage ' +
          pct(c.data_coverage) + " — not measured: " +
          c.parameters_missing.map(function (p) { return esc(paramName(p)); }).join(", ") + "</div>";
      }
      out += "</div></div>";
    });
    $("tab-cohorts").innerHTML = out;
  }

  function clusterDiseaseChips(id) {
    if (!state.config) return "";
    var c = state.config.cohorts.filter(function (x) { return x.id === id; })[0];
    if (!c) return "";
    return c.diseases.map(function (l) {
      return '<span class="chip chip-out" title="' + esc(l.dm_basis || "") + '">' + esc(l.name) +
        " · " + l.role + " " + l.weight + "</span>";
    }).join("");
  }

  /* reference-interval gauge: shows where the value sits relative to the range */
  function gauge(p) {
    if (p.value === null || p.value === undefined) return "";
    var lo = p.reference_low, hi = p.reference_high;
    if (lo === null && hi === null) return "";
    var l = lo === null ? hi * 0.5 : lo;
    var h = hi === null ? l * 2 : hi;
    if (!(h > l)) return "";
    var span = h - l, min = l - span * 0.9, max = h + span * 0.9;
    var v = p.value;
    if (v < min) min = v - span * 0.15;
    if (v > max) max = v + span * 0.15;
    var f = function (x) { return Math.max(0, Math.min(100, 100 * (x - min) / (max - min))); };
    var cls = p.direction === "high" ? "hi" : (p.direction === "low" ? "lo" : "");
    return '<div class="gauge"><div class="gauge-track"></div>' +
      '<div class="gauge-band" style="left:' + f(l) + "%;width:" + (f(h) - f(l)) + '%"></div>' +
      '<div class="gauge-mark ' + cls + '" style="left:' + f(v) + '%"></div></div>';
  }

  function flagFor(p) {
    if (!p.abnormal) return '<span class="flag flag-N">·</span>';
    if (p.direction === "high") return '<span class="flag flag-H">H</span>';
    if (p.direction === "low") return '<span class="flag flag-L">L</span>';
    return '<span class="flag flag-A">A</span>';
  }

  function paramTable(rows) {
    var out = '<div class="tbl-wrap"><table class="tbl"><thead><tr>' +
      '<th style="width:26px"></th><th>Parameter</th><th>Profile</th><th class="r">Result</th>' +
      "<th>Reference</th><th>Range position</th><th>Interpretation</th><th>Notes</th>" +
      "</tr></thead><tbody>";
    rows.forEach(function (p) {
      var val = p.kind === "qualitative"
        ? '<span class="chip ' + (p.status === "positive" ? "chip-crit" :
            p.status === "negative" ? "chip-ok" : "chip-warn") + '">' + esc(p.status) + "</span>"
        : '<span class="num">' + num(p.value) + "</span>" +
          (p.unit ? ' <span class="unit">' + esc(p.unit) + "</span>" : "");
      var ref = "—";
      if (p.reference_low !== null || p.reference_high !== null) {
        ref = (p.reference_low !== null ? num(p.reference_low) : "") +
              (p.reference_low !== null && p.reference_high !== null ? "–" : "") +
              (p.reference_high !== null ? num(p.reference_high) : "");
      }
      var notes = (p.notes || []).slice();
      if (p.conversion_note) notes.push(p.conversion_note);
      if (p.derived) notes.push(p.derivation);
      out += '<tr class="' + (p.abnormal ? "flagged" : "") + '">' +
        "<td>" + flagFor(p) + "</td>" +
        '<td><span class="name">' + esc(p.name) + "</span>" +
          (p.derived ? ' <span class="chip chip-out">derived</span>' : "") + "</td>" +
        '<td class="small muted">' + esc(p.profile || "—") + "</td>" +
        '<td class="r">' + val + "</td>" +
        '<td class="num muted">' + ref + '<div class="sub">' + esc(p.reference_source) + "</div></td>" +
        "<td>" + gauge(p) + "</td>" +
        '<td class="small">' + esc(p.grade_label || p.grade) + "</td>" +
        '<td class="small muted">' + notes.map(esc).join("<br>") + "</td></tr>";
    });
    return out + "</tbody></table></div>";
  }

  function renderParams(d) {
    var abn = d.parameters.filter(function (p) { return p.abnormal; });
    var norm = d.parameters.filter(function (p) { return !p.abnormal; });
    var out = '<div class="card"><div class="card-head">' +
      '<span class="card-title">Abnormal parameters</span>' +
      '<span class="card-note">' + abn.length + " of " + d.parameters.length + "</span></div>" +
      '<div class="card-body flush">' +
      (abn.length ? paramTable(abn)
        : '<div class="card-body"><span class="muted small">None — all recognised parameters are within range.</span></div>') +
      "</div></div>";
    out += '<div class="card"><div class="card-head">' +
      '<span class="card-title">Within reference range</span>' +
      '<span class="card-note">' + norm.length + "</span></div>" +
      '<div class="card-body flush">' +
      (norm.length ? paramTable(norm)
        : '<div class="card-body"><span class="muted small">None.</span></div>') +
      "</div></div>";
    $("tab-params").innerHTML = out;
  }

  function renderPlan(d) {
    if (!d.recommendations.length) {
      $("tab-plan").innerHTML = '<div class="empty"><div class="lead">No recommendations</div></div>';
      return;
    }
    var out = '<div class="card"><div class="card-body"><div class="prose muted">' +
      "Actions are ordered by priority. Disease-level guidance is taken verbatim from the Disease " +
      "Master's own <i>Prevention/Lifestyle Guidance</i> and <i>Recommended Next Step</i> columns. " +
      "Nothing here is a prescription or a treatment decision.</div></div></div>";

    var groups = {};
    d.recommendations.forEach(function (r) { (groups[r.priority] = groups[r.priority] || []).push(r); });
    ["urgent", "high", "medium", "low"].forEach(function (p) {
      if (!groups[p]) return;
      out += '<div class="rec-group"><div class="section-label">' + p + " priority · " +
        groups[p].length + "</div>";
      groups[p].forEach(function (r) {
        out += '<div class="rec p-' + r.priority + '"><div class="rec-mark"></div>' +
          '<div class="rec-cat">' + esc(r.category) + "</div>" +
          '<div class="rec-main"><div class="rec-text">' + esc(r.text) + "</div>" +
          '<div class="rec-why">' + esc(r.because) +
          ((r.sources || []).length ? " · " + r.sources.slice(0, 3).map(esc).join(", ") : "") +
          "</div></div></div>";
      });
      out += "</div>";
    });
    $("tab-plan").innerHTML = out;
  }

  function renderData(d) {
    var s = d.summary;
    var out = '<div class="card"><div class="card-head"><span class="card-title">Extraction</span></div>' +
      '<div class="card-body"><dl class="kv">' +
      "<dt>Observations found</dt><dd>" + s.observations_found + "</dd>" +
      "<dt>Mapped to parameters</dt><dd>" + s.parameters_recognised + "</dd>" +
      "<dt>Unmapped</dt><dd>" + s.parameters_unmapped + "</dd>" +
      "<dt>Profiles touched</dt><dd>" + s.profiles_touched.map(esc).join(", ") + "</dd>" +
      "</dl></div></div>";

    if (d.warnings && d.warnings.length) {
      out += '<div class="callout warn" style="margin:0 0 14px"><b>Extraction warnings</b>' +
        "<ul style=\"margin:5px 0 0;padding-left:18px\">" +
        d.warnings.map(function (w) { return "<li>" + esc(w) + "</li>"; }).join("") + "</ul></div>";
    }

    if (d.duplicates_resolved.length) {
      out += '<div class="card"><div class="card-head">' +
        '<span class="card-title">Duplicate results resolved</span>' +
        '<span class="card-note">' + d.duplicates_resolved.length + "</span></div>" +
        '<div class="card-body flush"><div class="tbl-wrap"><table class="tbl"><thead><tr>' +
        '<th>Parameter</th><th class="r">Seen</th><th class="r">Kept</th><th class="r">Dropped</th>' +
        "<th>Reason</th></tr></thead><tbody>";
      d.duplicates_resolved.forEach(function (x) {
        out += '<tr><td><span class="name">' + esc(x.parameter) + "</span>" +
          (x.conflicting_values ? ' <span class="chip chip-warn">values differed</span>' : "") + "</td>" +
          '<td class="r num">' + x.occurrences + "</td>" +
          '<td class="r num">' + esc(x.kept.value) + " " + esc(x.kept.unit || "") + "</td>" +
          '<td class="r num muted">' + x.dropped.map(function (v) {
            return esc(v.value) + " " + esc(v.unit || "");
          }).join("<br>") + "</td>" +
          '<td class="small muted">' + esc(x.reason) + "</td></tr>";
      });
      out += "</tbody></table></div></div></div>";
    }

    if (d.unmapped_observations.length) {
      out += '<div class="card"><div class="card-head">' +
        '<span class="card-title">Unmapped observations</span>' +
        '<span class="card-note">' + d.unmapped_observations.length + "</span></div>" +
        '<div class="card-body"><div class="prose muted small">These appeared in the file but ' +
        "matched no parameter in the dictionary. They were ignored rather than guessed at — add " +
        "an alias in <code>config/parameters.json</code> to recognise them.</div></div>" +
        '<div class="card-body flush"><div class="tbl-wrap"><table class="tbl"><thead><tr>' +
        '<th>Name in file</th><th class="r">Value</th><th>Unit</th><th>Location</th>' +
        "</tr></thead><tbody>";
      d.unmapped_observations.forEach(function (o) {
        out += "<tr><td>" + esc(o.raw_name) + '</td><td class="r num">' + esc(o.raw_value) +
          "</td><td>" + esc(o.raw_unit || "—") + '</td><td class="small muted">' +
          esc(o.source_path || "") + "</td></tr>";
      });
      out += "</tbody></table></div></div></div>";
    }

    if (d.cohorts_not_assessable.length) {
      out += '<div class="card"><div class="card-head">' +
        '<span class="card-title">Clusters not assessable</span>' +
        '<span class="card-note">' + d.cohorts_not_assessable.length + "</span></div>" +
        '<div class="card-body flush"><div class="tbl-wrap"><table class="tbl"><thead><tr>' +
        "<th>Cluster</th><th>Reason</th></tr></thead><tbody>";
      d.cohorts_not_assessable.forEach(function (c) {
        out += "<tr><td>" + esc(c.name) + '</td><td class="small muted">' + esc(c.reason) +
          "</td></tr>";
      });
      out += "</tbody></table></div></div></div>";
    }
    $("tab-data").innerHTML = out;
  }

  function renderReference() {
    if (!state.config) { $("tab-reference").innerHTML = '<div class="empty">Loading…</div>'; return; }
    var c = state.config, out = "";

    var distinct = {}, totalCites = 0, linkTotal = 0, linkBasis = 0, cross = 0;
    c.cohorts.forEach(function (x) {
      (x.evidence || []).forEach(function (e) { totalCites++; distinct[e.citation] = 1; });
      (x.diseases || []).forEach(function (l) { linkTotal++; if (l.dm_basis) linkBasis++; });
      if (x.cross_profile) cross++;
    });

    out += '<div class="metrics">' +
      metric(c.counts.diseases, "Conditions") +
      metric(c.counts.cohorts, "Clusters") +
      metric(Object.keys(distinct).length, "References") +
      metric(c.counts.parameters, "Parameters") +
      metric(c.counts.aliases, "Aliases") +
      metric(c.counts.disease_links, "Links") +
      "</div>";

    out += '<div class="card"><div class="card-head"><span class="card-title">Disease Master</span>' +
      '<span class="card-note">' + esc(c.disease_master.source_file) + "</span></div>" +
      '<div class="card-body"><dl class="kv">' +
      "<dt>Source sheet</dt><dd>" + esc(c.disease_master.sheet) + "</dd>" +
      "<dt>Conditions loaded</dt><dd>" + c.disease_master.disease_count + " of " +
        (c.disease_master.disease_count + c.disease_master.skipped_rows.length) + " rows</dd>" +
      "<dt>Rows skipped</dt><dd>" + (c.disease_master.skipped_rows.map(function (s) {
        return esc(s.name) + ' <span class="muted">(' + esc(s.reason) + ")</span>";
      }).join("<br>") || "none") + "</dd>" +
      "<dt>Conditions mapped</dt><dd>" + c.counts.diseases_linked + " of " + c.counts.diseases +
        " (" + Math.round(100 * c.counts.diseases_linked / c.counts.diseases) + "%)</dd>" +
      '<dt>Columns used</dt><dd class="small muted">' +
        c.disease_master.columns.map(esc).join(" · ") + "</dd>" +
      "</dl>" +
      '<div class="callout info">' + esc(c.disease_master.provenance_note) + "</div></div></div>";

    out += '<div class="card"><div class="card-head"><span class="card-title">Evidence basis</span>' +
      '<span class="card-note">enforced at load time</span></div><div class="card-body">' +
      '<div class="prose muted">Each cluster must cite the guideline or study that establishes its ' +
      "pattern, and each disease link must quote the Disease Master field it was matched on. A " +
      "cluster without a citation, or a link without a basis, is rejected as a configuration error." +
      "</div><dl class=\"kv\" style=\"margin-top:10px\">" +
      "<dt>Clusters citing a reference</dt><dd>" + c.cohorts.length + " of " + c.cohorts.length +
        " · " + Object.keys(distinct).length + " distinct references (" + totalCites + " citations)</dd>" +
      "<dt>Links quoting a Disease Master field</dt><dd>" + linkBasis + " of " + linkTotal + "</dd>" +
      "<dt>Cross-profile clusters</dt><dd>" + cross + " of " + c.cohorts.length + "</dd>" +
      "</dl></div></div>";

    out += '<div class="card"><div class="card-head">' +
      '<span class="card-title">Cluster library</span>' +
      '<span class="card-note">' + c.cohorts.length + " clusters · " + cross + " cross-profile</span>" +
      '</div><div class="card-body flush"><div class="tbl-wrap"><table class="tbl"><thead><tr>' +
      '<th>Cluster</th><th>Domain</th><th>Profiles</th><th>Maps to</th><th class="r">Refs</th>' +
      "</tr></thead><tbody>";
    c.cohorts.forEach(function (x) {
      out += '<tr><td><span class="name">' + esc(x.name) + "</span>" +
        (x.cross_profile ? ' <span class="chip chip-info">cross-profile</span>' : "") +
        '<div class="sub">' + esc(x.description || "") + "</div></td>" +
        '<td class="small">' + esc(x.domain || "") + "</td>" +
        '<td class="small muted">' + x.profiles_touched.map(esc).join("<br>") + "</td>" +
        '<td class="small">' + x.diseases.map(function (l) {
          return esc(l.name) + ' <span class="muted">(' + l.role + " " + l.weight + ")</span>";
        }).join("<br>") + "</td>" +
        '<td class="r num">' + (x.evidence || []).length + "</td></tr>";
    });
    out += "</tbody></table></div></div></div>";

    out += '<div class="card"><div class="card-head">' +
      '<span class="card-title">Not mappable from laboratory data</span>' +
      '<span class="card-note">' + c.unmappable.conditions.length + " conditions</span></div>" +
      '<div class="card-body"><div class="prose muted small">' + esc(c.unmappable.note) + "</div>";
    c.unmappable.conditions.forEach(function (u) {
      out += '<div class="cite"><b>' + esc(u.name) + "</b><br>" + esc(u.reason) +
        (u.would_need ? '<br><span class="muted">Would need: ' + esc(u.would_need) + "</span>" : "") +
        "</div>";
    });
    if (c.unmappable.notes_on_weak_links) {
      out += '<div class="small muted" style="margin-top:9px">' +
        esc(c.unmappable.notes_on_weak_links) + "</div>";
    }
    out += "</div></div>";

    out += '<div class="card"><div class="card-head">' +
      '<span class="card-title">Profile coverage in the Disease Master</span></div>' +
      '<div class="card-body flush"><div class="tbl-wrap"><table class="tbl"><thead><tr>' +
      '<th>Profile</th><th class="r">Tests</th><th class="r">Diseases mapped</th><th>Status</th>' +
      "</tr></thead><tbody>";
    c.profiles.forEach(function (p) {
      out += "<tr><td>" + esc(p["NG Profile (canonical)"]) + '</td><td class="r num">' +
        esc(p["# Tests in Profile"]) + '</td><td class="r num">' +
        esc(p["# Diseases Mapped (this master)"]) + "</td><td>" +
        esc(p["Coverage Status"]) + "</td></tr>";
    });
    out += "</tbody></table></div></div></div>";

    if (c.data_quality_notes && c.data_quality_notes.length) {
      out += '<div class="card"><div class="card-head">' +
        '<span class="card-title">Data quality notes from the workbook</span></div>' +
        '<div class="card-body">';
      c.data_quality_notes.forEach(function (n) {
        out += '<div class="cite"><b>' + esc(n.Observation) + "</b><br>" + esc(n.Detail) +
          '<br><span class="muted">' + esc(n.Recommendation) + "</span></div>";
      });
      out += "</div></div>";
    }
    $("tab-reference").innerHTML = out;
  }

  /* ------------------------------------------------ boot */

  fetch("/api/samples").then(function (r) { return r.json(); }).then(function (list) {
    $("sampleList").innerHTML = list.map(function (s) {
      return '<button data-file="' + esc(s.file) + '" data-label="' + esc(s.label) +
        '"><span class="kind">' + esc(s.kind || "JSON") + "</span><span>" +
        esc(s.label) + "</span></button>";
    }).join("");
    $("sampleList").addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (b) runSample(b.dataset.file, b.dataset.label);
    });
  }).catch(function () {});

  fetch("/api/config/summary").then(function (r) { return r.json(); }).then(function (c) {
    state.config = c;
    $("appbarMeta").innerHTML =
      '<div class="meta-item"><b>' + c.counts.diseases + "</b>conditions</div>" +
      '<div class="meta-sep"></div>' +
      '<div class="meta-item"><b>' + c.counts.cohorts + "</b>clusters</div>" +
      '<div class="meta-sep"></div>' +
      '<div class="meta-item"><b>' + c.counts.parameters + "</b>parameters</div>" +
      '<div class="meta-sep"></div>' +
      '<div class="meta-item">' + esc(c.disease_master.source_file) + "</div>";
    $("sidebarFoot").innerHTML = c.counts.disease_links + " cluster→disease links<br>" +
      c.counts.aliases + " parameter aliases<br>" +
      c.counts.diseases_linked + "/" + c.counts.diseases + " conditions mapped" +
      '<br><a href="/">Main interface</a>';
    renderReference();
  }).catch(function () {});
})();
