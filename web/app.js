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
  function num(v) {
    if (v === null || v === undefined) return "—";
    if (typeof v !== "number") return esc(v);
    if (Math.abs(v) >= 10000) return v.toLocaleString();
    return String(Math.round(v * 10000) / 10000);
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

  function runSample(fileName, label) {
    var fd = new FormData();
    fd.append("file", fileName);
    if ($("sexInput").value) fd.append("sex", $("sexInput").value);
    $("chosen").innerHTML = "Sample: <b>" + esc(label) + "</b>";
    send("/api/analyse/sample", fd);
  }

  function send(url, fd) {
    $("analyseBtn").disabled = true;
    status('<span class="spin"></span>Extracting parameters, detecting clusters and scoring against the Disease Master…');
    fetch(url, { method: "POST", body: fd })
      .then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body.detail || ("HTTP " + r.status));
          return body;
        });
      })
      .then(function (data) {
        state.result = data;
        status("Analysis complete — " + data.summary.parameters_recognised +
               " parameters recognised, " + data.summary.cohorts_detected +
               " clusters detected, " + data.summary.conditions_flagged + " conditions flagged.", "info");
        render();
        $("results").hidden = false;
        $("results").scrollIntoView({ behavior: "smooth", block: "start" });
      })
      .catch(function (e) { status("Could not analyse this file: " + esc(e.message), "error"); })
      .finally(function () { $("analyseBtn").disabled = !state.file; });
  }

  /* ---------------- tabs ---------------- */

  $("tabs").addEventListener("click", function (e) {
    var b = e.target.closest(".tab");
    if (!b) return;
    document.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
    document.querySelectorAll(".panel").forEach(function (p) { p.classList.remove("active"); });
    b.classList.add("active");
    $("tab-" + b.dataset.tab).classList.add("active");
  });

  /* ---------------- render ---------------- */

  function render() {
    var d = state.result;
    $("pillRisks").textContent = d.disease_risks.length;
    $("pillCohorts").textContent = d.cohorts.length;
    $("pillParams").textContent = d.parameters.length;
    $("pillPlan").textContent = d.recommendations.length;

    renderOverview(d);
    renderRisks(d);
    renderCohorts(d);
    renderParams(d);
    renderPlan(d);
    renderData(d);
    renderReference();
  }

  function patientLine(p) {
    var bits = [];
    if (p.patient_id) bits.push("ID " + esc(p.patient_id));
    if (p.name) bits.push(esc(p.name));
    if (p.sex) bits.push(esc(p.sex));
    if (p.age) bits.push(Math.round(p.age) + " yrs");
    if (p.report_date) bits.push(esc(p.report_date));
    return bits.length ? bits.join(" · ") : "no patient details stated in the file";
  }

  function renderOverview(d) {
    var s = d.summary, out = "";

    out += '<div class="card"><h2>Record</h2><dl class="kv">' +
      "<dt>Patient</dt><dd>" + patientLine(d.patient) + "</dd>" +
      "<dt>Source file</dt><dd>" + esc(d.source_file) + "</dd>" +
      "<dt>Analysed</dt><dd>" + esc(d.generated_at.replace("T", " ")) + "</dd>" +
      "<dt>Analysis confidence</dt><dd><b>" + esc(s.analysis_confidence) + "</b> — " +
        esc(d.coverage.note) + "</dd>" +
      "<dt>Disease Master</dt><dd>" + esc(d.provenance.disease_master_source) +
        " — " + d.provenance.disease_master_rows + " conditions, " +
        d.provenance.cohorts_configured + " clusters, " +
        d.provenance.evidence_citations + " cited references</dd>" +
      "</dl></div>";

    out += '<div class="stats">' +
      stat(s.parameters_recognised, "parameters recognised") +
      stat(s.abnormal_count, "abnormal", s.abnormal_count ? "alert" : "ok") +
      stat(s.cohorts_detected, "clusters detected") +
      stat(s.high_evidence, "high evidence", s.high_evidence ? "alert" : "") +
      stat(s.moderate_evidence, "moderate evidence", s.moderate_evidence ? "warn" : "") +
      stat(s.limited_evidence, "low / limited") +
      "</div>";

    if (d.urgent_findings.length) {
      out += '<div class="callout urgent"><b>Time-critical findings.</b> ' +
        d.urgent_findings.map(function (r) { return esc(r.name); }).join(", ") +
        " — the Disease Master marks these as needing immediate assessment. " +
        "Seek medical care now rather than waiting for a routine appointment.</div>";
    }

    if (!d.disease_risks.length) {
      out += '<div class="empty"><div class="big">✓</div>' +
        "<b>No disease risks were flagged from the parameters available.</b><br>" +
        (s.abnormal_count
          ? "Some parameters are outside their reference range but they do not form any of the clinically established clusters this engine detects."
          : "All recognised parameters fall within their reference ranges.") +
        "</div>";
    } else {
      out += '<div class="card"><h2>Top findings</h2>';
      d.disease_risks.slice(0, 5).forEach(function (r) {
        out += '<div class="contrib"><span class="badge b-' + r.evidence_level + '">' +
          r.evidence_level + "</span><b style=\"flex:0 0 auto\">" + esc(r.name) + "</b>" +
          '<div class="bar"><i style="width:' + pct(r.score) + '"></i></div>' +
          '<span class="w">' + r.score.toFixed(2) + "</span></div>";
      });
      out += '<p class="small muted" style="margin:12px 0 0">Open the <b>Disease Risks</b> tab for the ' +
        "reasoning, triggering parameters and missing information behind each one.</p></div>";
    }

    if (d.coverage.capped_note) {
      out += '<div class="callout warn"><b>Limited evidence.</b> ' + esc(d.coverage.capped_note) +
        " Affected: " + d.coverage.capped_conditions.map(esc).join(", ") + ".</div>";
    }

    out += '<div class="callout info">' + esc(d.disclaimer) + "</div>";
    $("tab-overview").innerHTML = out;
  }

  function stat(n, label, cls) {
    return '<div class="stat ' + (cls || "") + '"><div class="n">' + n +
      '</div><div class="l">' + label + "</div></div>";
  }

  function renderRisks(d) {
    if (!d.disease_risks.length) {
      $("tab-risks").innerHTML = '<div class="empty"><div class="big">✓</div>' +
        "No conditions reached the reporting threshold.</div>";
      return;
    }
    var out = '<p class="small muted" style="margin:0 0 14px">' +
      "Each condition below is a Disease Master row. Click one to see the clusters that " +
      "triggered it, the exact Disease Master field that justifies the link, the parameters " +
      "involved, and what is missing.</p>";

    d.disease_risks.forEach(function (r, i) {
      out += '<div class="risk lv-' + r.evidence_level + '" data-i="' + i + '">';
      out += '<div class="risk-head"><div><div class="risk-title">' + esc(r.name) + "</div>";
      out += '<div class="risk-meta">' +
        '<span class="badge b-' + r.evidence_level + '">' + r.evidence_level + " evidence</span>" +
        '<span class="badge b-' + r.urgency_tier + '">' + esc(r.urgency_tier) + "</span>" +
        '<span class="badge b-tag">' + esc(r.classification) + "</span>" +
        (r.icd10 ? '<span class="badge b-tag">ICD-10 ' + esc(r.icd10) + "</span>" : "") +
        (r.evidence_capped ? '<span class="badge b-Limited">capped — thin data</span>' : "") +
        '<span class="small muted">' + r.profiles.map(esc).join(" · ") + "</span>" +
        "</div></div>";
      out += '<div class="risk-score"><div class="v">' + r.score.toFixed(2) +
        '</div><div class="c">evidence</div></div></div>';

      out += '<div class="risk-body">';

      out += '<div class="section"><h4>Why this was flagged</h4>' +
        '<div class="explain">' + esc(r.explanation) + "</div></div>";

      out += '<div class="section"><h4>Evidence contributions (combined with noisy-OR)</h4>';
      r.contributions.forEach(function (c) {
        out += '<div class="contrib"><span class="role-tag">' + esc(c.role) + "</span>" +
          "<span style=\"flex:0 0 auto\">" + esc(c.cohort_name) + "</span>" +
          '<div class="bar"><i style="width:' + pct(c.contribution) + '"></i></div>' +
          '<span class="w">' + c.contribution.toFixed(3) +
          "  (w " + c.link_weight + " × conf " + c.cohort_confidence.toFixed(2) + ")</span></div>";
        if (c.dm_basis) out += '<div class="basis">Disease Master basis — ' + esc(c.dm_basis) + "</div>";
      });
      out += "</div>";

      var trig = r.triggering_parameters.filter(function (t) { return !t.discounted; });
      if (trig.length) {
        out += '<div class="section"><h4>Triggering parameters</h4><div class="chips">' +
          trig.map(function (t) {
            return '<span class="chip trig" title="' + esc(t.observed || "") + " — via " +
              esc(t.via_cohort) + '">' + esc(t.name) + "</span>";
          }).join("") + "</div></div>";
      }

      if (r.missing_parameters.length) {
        out += '<div class="section"><h4>Not measured — testing these would raise confidence</h4>' +
          '<div class="chips">' + r.missing_parameters.map(function (p) {
            return '<span class="chip miss">' + esc(paramName(p)) + "</span>";
          }).join("") + "</div>" +
          '<p class="small muted" style="margin:8px 0 0">Data coverage for this condition: <b>' +
          pct(r.data_coverage) + "</b> of its relevant markers.</p></div>";
      }

      if (r.conditional_urgency && r.conditional_urgency !== r.urgency_tier) {
        out += '<div class="callout warn"><b>Can escalate.</b> The Disease Master notes this ' +
          "condition can become <b>" + esc(r.conditional_urgency) + "</b>-level: " +
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
      out += "</dl></details>";

      out += "</div></div>";
    });
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

  function renderCohorts(d) {
    if (!d.cohorts.length) {
      $("tab-cohorts").innerHTML = '<div class="empty"><div class="big">—</div>' +
        "No clinically established clusters were detected in these parameters.</div>";
      return;
    }
    var out = '<p class="small muted" style="margin:0 0 14px">A cluster is a combination of ' +
      "parameters that, together, carry clinical meaning that none of them carries alone. " +
      "Clusters marked <b>cross-profile</b> deliberately draw on more than one test profile.</p>";

    d.cohorts.forEach(function (c) {
      out += '<div class="cohort"><div class="cohort-head"><div>' +
        "<h3>" + esc(c.name) + "</h3>" +
        '<div class="risk-meta">' +
        '<span class="badge b-tag">' + esc(c.category) + "</span>" +
        (c.cross_profile_rationale ? '<span class="badge b-monitoring">cross-profile</span>' : "") +
        (c.mode === "count_of" ? '<span class="badge b-tag">counting rule</span>' : "") +
        (c.urgency_override ? '<span class="badge b-' + c.urgency_override + '">' +
          esc(c.urgency_override) + "</span>" : "") +
        '<span class="small muted">' + c.profiles_touched.map(esc).join(" · ") + "</span>" +
        "</div></div>" +
        '<div class="conf">' + pct(c.confidence) + '<div class="c small muted" ' +
        'style="text-align:right;font-family:inherit">confidence</div></div></div>';

      out += '<div class="desc">' + esc(c.description) + "</div>";

      if (c.mode === "count_of") {
        out += '<div class="section"><h4>Components met (' + c.components_met.length + ")</h4>";
        c.components_met.forEach(function (m) {
          out += '<div class="trigger-row"><span class="role-tag">met</span>' +
            '<span class="lbl">' + esc(m.label) + " — " + esc(m.evidence) + "</span></div>";
        });
        c.components_unmet.forEach(function (m) {
          out += '<div class="trigger-row dim"><span class="role-tag">' +
            (m.status.indexOf("not assessable") === 0 ? "no data" : "not met") + "</span>" +
            '<span class="lbl">' + esc(m.label) + " — " + esc(m.status) + "</span></div>";
        });
        out += "</div>";
      }

      var hits = c.hits.filter(function (h) { return h.effective_weight > 0; });
      if (hits.length) {
        out += '<div class="section"><h4>Signals</h4>';
        hits.forEach(function (h) {
          out += '<div class="trigger-row' + (h.suppressed_by ? " dim" : "") + '">' +
            '<span class="role-tag">' + esc(h.role) + "</span>" +
            '<span class="lbl">' + esc(h.label || h.parameter_name) +
            ' <span class="muted">— ' + esc(h.observed) + "</span>" +
            (h.suppressed_by ? '<br><span class="small muted">weight reduced to avoid ' +
              "double-counting with " + esc(h.suppressed_by) + "</span>" : "") +
            '</span><span class="w num">' + h.effective_weight.toFixed(2) + "</span></div>";
        });
        out += "</div>";
      }

      if (c.cross_profile_rationale) {
        out += '<div class="section"><h4>Why this crosses profiles</h4>' +
          '<div class="explain">' + esc(c.cross_profile_rationale) + "</div></div>";
      }

      out += '<div class="section"><h4>Maps to</h4><div class="chips">' +
        (state.config ? cohortDiseaseChips(c.cohort_id) : "") + "</div></div>";

      if (c.evidence && c.evidence.length) {
        out += "<details><summary>Clinical references (" + c.evidence.length + ")</summary>";
        c.evidence.forEach(function (e) {
          out += '<div class="cite"><b>' + esc(e.citation) + "</b><br>" + esc(e.note) + "</div>";
        });
        out += "</details>";
      }

      if (c.parameters_missing.length) {
        out += '<p class="small muted" style="margin:10px 0 0">Data coverage ' +
          pct(c.data_coverage) + " — not measured: " +
          c.parameters_missing.map(function (p) { return esc(paramName(p)); }).join(", ") + "</p>";
      }
      out += "</div>";
    });
    $("tab-cohorts").innerHTML = out;
  }

  function cohortDiseaseChips(cohortId) {
    var c = state.config.cohorts.filter(function (x) { return x.id === cohortId; })[0];
    if (!c) return "";
    return c.diseases.map(function (l) {
      return '<span class="chip" title="' + esc(l.dm_basis || "") + '">' + esc(l.name) +
        ' <span class="muted">' + l.role + " · w " + l.weight + "</span></span>";
    }).join("");
  }

  function renderParams(d) {
    var abn = d.parameters.filter(function (p) { return p.abnormal; });
    var norm = d.parameters.filter(function (p) { return !p.abnormal; });
    var out = '<div class="card"><h2>Abnormal parameters (' + abn.length + ")</h2>";
    out += abn.length ? paramTable(abn) : '<p class="muted small">None — all recognised parameters are within range.</p>';
    out += "</div>";
    out += '<div class="card"><h2>Within range (' + norm.length + ")</h2>" +
      (norm.length ? paramTable(norm) : '<p class="muted small">None.</p>') + "</div>";
    $("tab-params").innerHTML = out;
  }

  function paramTable(rows) {
    var out = '<div class="wrap"><table class="tbl"><thead><tr>' +
      "<th>Parameter</th><th>Profile</th><th>Result</th><th>Reference</th>" +
      "<th>Interpretation</th><th>Notes</th></tr></thead><tbody>";
    rows.forEach(function (p) {
      var value = p.kind === "qualitative"
        ? '<span class="badge b-' + (p.status || "normal") + '">' + esc(p.status) + "</span>"
        : '<span class="num">' + num(p.value) + "</span> " +
          '<span class="muted small">' + esc(p.unit || "") + "</span>";
      var ref = "—";
      if (p.reference_low !== null || p.reference_high !== null) {
        ref = (p.reference_low !== null ? num(p.reference_low) : "") +
              (p.reference_low !== null && p.reference_high !== null ? " – " : "") +
              (p.reference_high !== null ? num(p.reference_high) : "");
        ref = '<span class="num">' + ref + "</span>";
      }
      var notes = (p.notes || []).slice();
      if (p.conversion_note) notes.push(p.conversion_note);
      if (p.derived) notes.push(p.derivation);
      out += '<tr class="' + (p.abnormal ? "abn" : "") + '">' +
        "<td><b>" + esc(p.name) + "</b>" +
          (p.derived ? ' <span class="badge b-tag">derived</span>' : "") + "</td>" +
        '<td class="small muted">' + esc(p.profile || "—") + "</td>" +
        "<td>" + value + "</td>" +
        "<td>" + ref + '<div class="small muted">' + esc(p.reference_source) + "</div></td>" +
        "<td>" + (p.abnormal
          ? '<span class="badge b-' + (p.direction || "high") + '">' + esc(p.direction || "") + "</span> "
          : "") + '<span class="small">' + esc(p.grade_label || p.grade) + "</span></td>" +
        '<td class="small muted">' + notes.map(esc).join("<br>") + "</td></tr>";
    });
    return out + "</tbody></table></div>";
  }

  function renderPlan(d) {
    if (!d.recommendations.length) { $("tab-plan").innerHTML = '<div class="empty">No recommendations.</div>'; return; }
    var out = '<p class="small muted" style="margin:0 0 14px">Actions are ordered by priority. ' +
      "Disease-level guidance is taken verbatim from the Disease Master's own " +
      "<i>Prevention/Lifestyle Guidance</i> and <i>Recommended Next Step</i> columns. " +
      "Nothing here is a prescription or a treatment decision.</p>";
    var groups = {};
    d.recommendations.forEach(function (r) { (groups[r.priority] = groups[r.priority] || []).push(r); });
    ["urgent", "high", "medium", "low"].forEach(function (p) {
      if (!groups[p]) return;
      out += '<h3 style="margin:16px 0 10px;font-size:12px;text-transform:uppercase;' +
        'letter-spacing:.06em;color:#788396">' + p + " priority (" + groups[p].length + ")</h3>";
      groups[p].forEach(function (r) {
        out += '<div class="rec p-' + r.priority + '"><div class="rec-top">' +
          '<span class="badge b-tag">' + esc(r.category) + "</span>" +
          (r.sources || []).slice(0, 3).map(function (s) {
            return '<span class="small muted">' + esc(s) + "</span>";
          }).join('<span class="muted">·</span>') +
          '</div><div class="rec-text">' + esc(r.text) + "</div>" +
          '<div class="rec-why">Triggered by: ' + esc(r.because) + "</div></div>";
      });
    });
    $("tab-plan").innerHTML = out;
  }

  function renderData(d) {
    var out = '<div class="card"><h2>Extraction</h2><dl class="kv">' +
      "<dt>Observations found</dt><dd>" + d.summary.observations_found + "</dd>" +
      "<dt>Mapped to parameters</dt><dd>" + d.summary.parameters_recognised + "</dd>" +
      "<dt>Unmapped</dt><dd>" + d.summary.parameters_unmapped + "</dd>" +
      "<dt>Profiles touched</dt><dd>" + d.summary.profiles_touched.map(esc).join(", ") + "</dd>" +
      "</dl></div>";

    if (d.warnings && d.warnings.length) {
      out += '<div class="callout warn"><b>Extraction warnings</b><ul style="margin:6px 0 0">' +
        d.warnings.map(function (w) { return "<li>" + esc(w) + "</li>"; }).join("") + "</ul></div>";
    }

    if (d.duplicates_resolved.length) {
      out += '<div class="card"><h2>Duplicate results resolved (' + d.duplicates_resolved.length + ")</h2>";
      out += '<div class="wrap"><table class="tbl"><thead><tr><th>Parameter</th><th>Seen</th>' +
        "<th>Kept</th><th>Dropped</th><th>Reason</th></tr></thead><tbody>";
      d.duplicates_resolved.forEach(function (x) {
        out += "<tr><td><b>" + esc(x.parameter) + "</b>" +
          (x.conflicting_values ? ' <span class="badge b-Moderate">values differed</span>' : "") + "</td>" +
          "<td>" + x.occurrences + "</td>" +
          '<td class="num">' + esc(x.kept.value) + " " + esc(x.kept.unit || "") + "</td>" +
          '<td class="num muted">' + x.dropped.map(function (v) {
            return esc(v.value) + " " + esc(v.unit || "");
          }).join("<br>") + "</td>" +
          '<td class="small muted">' + esc(x.reason) + "</td></tr>";
      });
      out += "</tbody></table></div></div>";
    }

    if (d.unmapped_observations.length) {
      out += '<div class="card"><h2>Unmapped observations (' + d.unmapped_observations.length + ")</h2>" +
        '<p class="small muted">These appeared in the file but did not match any parameter in the ' +
        "dictionary. They were ignored rather than guessed at. Add an alias in " +
        "<code>config/parameters.json</code> to recognise them.</p>" +
        '<div class="wrap"><table class="tbl"><thead><tr><th>Name in file</th><th>Value</th>' +
        "<th>Unit</th><th>Where</th></tr></thead><tbody>";
      d.unmapped_observations.forEach(function (o) {
        out += "<tr><td>" + esc(o.raw_name) + '</td><td class="num">' + esc(o.raw_value) +
          "</td><td>" + esc(o.raw_unit || "—") + '</td><td class="small muted">' +
          esc(o.source_path || "") + "</td></tr>";
      });
      out += "</tbody></table></div></div>";
    }

    if (d.cohorts_not_assessable.length) {
      out += '<div class="card"><h2>Clusters that could not be assessed</h2>' +
        '<p class="small muted">Not enough of their parameters were present in this record.</p>' +
        '<div class="wrap"><table class="tbl"><thead><tr><th>Cluster</th><th>Reason</th>' +
        "</tr></thead><tbody>";
      d.cohorts_not_assessable.forEach(function (c) {
        out += "<tr><td>" + esc(c.name) + '</td><td class="small muted">' + esc(c.reason) + "</td></tr>";
      });
      out += "</tbody></table></div></div>";
    }
    $("tab-data").innerHTML = out;
  }

  function renderReference() {
    if (!state.config) { $("tab-reference").innerHTML = '<div class="empty">Loading…</div>'; return; }
    var c = state.config, out = "";
    out += '<div class="card"><h2>Disease Master</h2><dl class="kv">' +
      "<dt>Source workbook</dt><dd>" + esc(c.disease_master.source_file) + " · sheet “" +
        esc(c.disease_master.sheet) + "”</dd>" +
      "<dt>Conditions loaded</dt><dd>" + c.disease_master.disease_count + " of " +
        (c.disease_master.disease_count + c.disease_master.skipped_rows.length) + " rows</dd>" +
      "<dt>Rows skipped</dt><dd>" + (c.disease_master.skipped_rows.map(function (s) {
        return esc(s.name) + " <span class='muted'>(" + esc(s.reason) + ")</span>";
      }).join("<br>") || "none") + "</dd>" +
      "<dt>Columns used</dt><dd class='small'>" + c.disease_master.columns.map(esc).join(" · ") + "</dd>" +
      "<dt>Conditions mapped</dt><dd>" + c.counts.diseases_linked + " of " + c.counts.diseases +
        " (" + Math.round(100 * c.counts.diseases_linked / c.counts.diseases) + "%)</dd>" +
      "<dt>Cohort→disease links</dt><dd>" + c.counts.disease_links + "</dd>" +
      "<dt>Parameters / aliases</dt><dd>" + c.counts.parameters + " / " + c.counts.aliases + "</dd>" +
      "</dl>" +
      '<div class="callout info" style="margin-top:14px">' + esc(c.disease_master.provenance_note) + "</div>" +
      "</div>";

    var totalCites = 0, distinct = {};
    c.cohorts.forEach(function (x) {
      (x.evidence || []).forEach(function (e) { totalCites++; distinct[e.citation] = 1; });
    });
    var linkTotal = 0, linkBasis = 0;
    c.cohorts.forEach(function (x) {
      (x.diseases || []).forEach(function (l) { linkTotal++; if (l.dm_basis) linkBasis++; });
    });
    out += '<div class="card"><h2>Evidence basis</h2>' +
      '<p class="small muted">Each cluster asserts a clinical relationship, so it must cite the ' +
      'guideline or study that establishes it, and each disease link must quote the Disease Master ' +
      'field it was matched on. Both are enforced at load time — a cluster without a citation, or a ' +
      'link without a basis, is rejected as a configuration error.</p><dl class="kv">' +
      "<dt>Clusters citing a reference</dt><dd>" + Object.keys(distinct).length +
        " distinct references across " + c.cohorts.length + " clusters (" + totalCites + " citations)</dd>" +
      "<dt>Links quoting a Disease Master field</dt><dd>" + linkBasis + " of " + linkTotal + "</dd>" +
      "</dl></div>";

    out += '<div class="card"><h2>Deliberately unmapped</h2>' +
      '<p class="small muted">' + esc(c.unmappable.note) + "</p>";
    c.unmappable.conditions.forEach(function (u) {
      out += '<div class="cite"><b>' + esc(u.name) + "</b><br>" + esc(u.reason) +
        (u.would_need ? '<br><span class="muted">Would need: ' + esc(u.would_need) + "</span>" : "") +
        "</div>";
    });
    if (c.unmappable.notes_on_weak_links) {
      out += '<p class="small muted" style="margin-top:10px">' +
        esc(c.unmappable.notes_on_weak_links) + "</p>";
    }
    out += "</div>";

    var cross = c.cohorts.filter(function (x) { return x.cross_profile; });
    out += '<div class="card"><h2>Cluster library (' + c.cohorts.length + ")</h2>" +
      '<p class="small muted">' + cross.length + " of these deliberately draw parameters from more " +
      "than one test profile, which is what lets a heart-risk cluster use lipid, metabolic, " +
      "glucose and inflammatory markers together.</p>" +
      '<div class="wrap"><table class="tbl"><thead><tr><th>Cluster</th><th>Domain</th>' +
      "<th>Profiles</th><th>Maps to</th><th>Refs</th></tr></thead><tbody>";
    c.cohorts.forEach(function (x) {
      out += "<tr><td><b>" + esc(x.name) + "</b>" +
        (x.cross_profile ? ' <span class="badge b-monitoring">cross-profile</span>' : "") +
        '<div class="small muted">' + esc(x.description || "") + "</div></td>" +
        '<td class="small">' + esc(x.domain || "") + "</td>" +
        '<td class="small muted">' + x.profiles_touched.map(esc).join("<br>") + "</td>" +
        '<td class="small">' + x.diseases.map(function (l) {
          return esc(l.name) + ' <span class="muted">(' + l.role + " " + l.weight + ")</span>";
        }).join("<br>") + "</td>" +
        '<td class="small muted">' + (x.evidence || []).length + "</td></tr>";
    });
    out += "</tbody></table></div></div>";

    out += '<div class="card"><h2>Profile coverage in the Disease Master</h2><div class="wrap">' +
      '<table class="tbl"><thead><tr><th>Profile</th><th>Tests</th><th>Diseases mapped</th>' +
      "<th>Status</th></tr></thead><tbody>";
    c.profiles.forEach(function (p) {
      out += "<tr><td>" + esc(p["NG Profile (canonical)"]) + '</td><td class="num">' +
        esc(p["# Tests in Profile"]) + '</td><td class="num">' +
        esc(p["# Diseases Mapped (this master)"]) + "</td><td>" + esc(p["Coverage Status"]) + "</td></tr>";
    });
    out += "</tbody></table></div></div>";

    if (c.data_quality_notes && c.data_quality_notes.length) {
      out += '<div class="card"><h2>Data quality notes carried from the workbook</h2>';
      c.data_quality_notes.forEach(function (n) {
        out += '<div class="cite"><b>' + esc(n.Observation) + "</b><br>" + esc(n.Detail) +
          "<br><i>" + esc(n.Recommendation) + "</i></div>";
      });
      out += "</div>";
    }
    $("tab-reference").innerHTML = out;
  }

  /* ---------------- boot ---------------- */

  fetch("/api/samples").then(function (r) { return r.json(); }).then(function (list) {
    $("sampleList").innerHTML = list.map(function (s) {
      return '<button data-file="' + esc(s.file) + '">' + esc(s.label) + "</button>";
    }).join("");
    $("sampleList").addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (b) runSample(b.dataset.file, b.textContent);
    });
  }).catch(function () {});

  fetch("/api/config/summary").then(function (r) { return r.json(); }).then(function (c) {
    state.config = c;
    $("topmeta").innerHTML = "<b>" + c.counts.diseases + "</b> conditions · <b>" +
      c.counts.cohorts + "</b> clusters · <b>" + c.counts.parameters + "</b> parameters<br>" +
      '<span class="muted">Disease Master: ' + esc(c.disease_master.source_file) + "</span>";
    $("footConfig").textContent = c.counts.disease_links + " cohort→disease links · " +
      c.counts.aliases + " parameter aliases · " + c.counts.diseases_linked + "/" +
      c.counts.diseases + " conditions mapped";
    if (state.result) renderReference();
  }).catch(function () {});
})();
