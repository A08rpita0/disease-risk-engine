"""Configuration loading and validation.

Everything clinical lives in config/ as JSON. This module loads it, builds the alias
index used by normalization, and validates cross-references so a bad edit fails loudly
at start-up instead of silently dropping a disease link.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def norm_key(text):
    """Aggressive normalisation used for alias matching.

    Lowercases, strips accents, removes anything that is not a letter, digit or space,
    and collapses whitespace. 'HDL-Cholesterol (Direct)' and 'hdl cholesterol direct'
    both become 'hdl cholesterol direct'.
    """
    if text is None:
        return ""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = text.lower()
    text = re.sub(r"[‐-―]", " ", text)
    text = re.sub(r"[^a-z0-9%/+]+", " ", text)
    # Fold British spellings onto one form so a report saying 'Glycosylated Haemoglobin'
    # matches an alias written as 'glycosylated hemoglobin'.
    for a, b in (("haemo", "hemo"), ("anaemi", "anemi"), ("leuco", "leuko"),
                 ("oestr", "estr"), ("sulph", "sulf"), ("ionis", "ioniz"),
                 ("foetal", "fetal"), ("caeruloplasmin", "ceruloplasmin"),
                 ("gonorrhoea", "gonorrhea"), ("diarrhoea", "diarrhea")):
        text = text.replace(a, b)
    # Drop honorific/specimen prefixes and filler words that labs add freely.
    # 'S. Creatinine', 'Serum Creatinine' and 'Creatinine' must all land on one key.
    unstripped = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\b(serum|plasma|blood|test|level|levels|value|result|estimation)\b",
                  " ", text)
    # ...unless that leaves only a specimen word. "Urine Blood" is the urine blood test;
    # stripping "blood" stored its alias as bare "urine", so every unrecognised
    # "Urine ..." name - "Urine Routine", "Urine Culture" - fell back onto it.
    if re.sub(r"\s+", " ", text).strip() in ("urine", "stool", "semen", "csf", "sputum",
                                             "saliva", "swab", ""):
        text = unstripped
    # "S." and "B." are specimen PREFIXES ("S. Creatinine", "B. Glucose") and only ever
    # stripped at the start. Stripping the letter anywhere turned "Apo B" into "apo" -
    # so a bare "Apolipoprotein" resolved to ApoB - and "Influenza B" into "influenza".
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:s|b)\s+", "", text)
    return text


# Words laboratories use interchangeably for the same thing. Folded before the fallback
# lookup only - never used to decide between two exact aliases. Each pair is a naming
# variant of ONE assay; nothing here merges two different tests.
_NAME_SYNONYMS = [
    (re.compile(r"\b(?:highly|high|hi)\s+sensitiv(?:e|ity)\b"), "hs"),
    (re.compile(r"\bc\s*reactive\s+protein\b"), "crp"),
    (re.compile(r"\bglycosylated\b"), "glycated"),
    (re.compile(r"\bapolipoproteins?\b"), "apo"),
    (re.compile(r"\bvit\b"), "vitamin"),
    (re.compile(r"\bhaemoglobin\b"), "hemoglobin"),
]
# Word order may vary ("C-Reactive Protein, High Sensitivity") EXCEPT where order is the
# meaning: LDL/HDL and HDL/LDL are reciprocal ratios.
_ORDERED = re.compile(r"\bratio\b|/|\bindex\b|\bper\b")


def canon_keys(nk):
    """Fallback lookup keys for an already-normalised name.

    Returns (compact, bag): synonyms folded, then either all spaces removed - "HsCRP"
    and "hs crp" meet at "hscrp" - or the words sorted, so that "c reactive protein high
    sensitivity" meets "high sensitivity c reactive protein". The bag form is withheld
    for ratios and indices, where word order carries the meaning.
    """
    if not nk:
        return None, None
    folded = nk
    for pat, rep in _NAME_SYNONYMS:
        folded = pat.sub(rep, folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    compact = re.sub(r"[\s/]+", "", folded)
    bag = None if _ORDERED.search(folded) else " ".join(sorted(folded.split()))
    return compact, bag


def norm_unit(unit):
    if unit is None:
        return None
    u = unicodedata.normalize("NFKD", str(unit)).strip().lower()
    u = u.replace("μ", "u").replace("µ", "u")
    u = u.replace(" ", "")
    u = u.replace("percent", "%")
    u = re.sub(r"^\(|\)$", "", u)
    # "-", "---", "NA": a unit column with nothing in it. Treated as no unit rather than
    # an unrecognised one, which would stop pH and specific gravity being interpreted.
    if u in ("-", "--", "---", "\u2014", "\u2013", "na", "n/a", "nil", ".", "_"):
        return None
    return u or None


class ConfigError(Exception):
    pass


class Config:
    """Loaded, indexed and validated clinical configuration."""

    def __init__(self, config_dir=None):
        self.dir = Path(config_dir) if config_dir else CONFIG_DIR
        self._load()
        self._index()
        self.validation = self.validate()

    # ---------- loading ----------

    def _read(self, name):
        path = self.dir / name
        if not path.exists():
            raise ConfigError("missing config file: %s" % path)
        return json.loads(path.read_text(encoding="utf-8"))

    def _load(self):
        dm = self._read("disease_master.json")
        self.dm_meta = dm["meta"]
        self.diseases = dm["diseases"]
        self.dm_profiles = dm.get("profiles", [])
        self.dm_legend = dm.get("field_legend", [])
        self.dm_notes = dm.get("data_quality_notes", [])
        self.dm_unclear = dm.get("unclear_mappings_resolved", [])

        pm = self._read("parameters.json")
        self.param_meta = pm["meta"]
        self.parameters = pm["parameters"]
        self.qual_vocab = pm["qualitative_vocabulary"]

        self.cohorts = []
        cohort_dir = self.dir / "cohorts"
        if not cohort_dir.is_dir():
            raise ConfigError("missing config/cohorts directory")
        for f in sorted(cohort_dir.glob("*.json")):
            block = json.loads(f.read_text(encoding="utf-8"))
            for c in block.get("cohorts", []):
                c.setdefault("domain", block.get("domain"))
                c["_source_file"] = f.name
                self.cohorts.append(c)

        self.unmappable = self._read("unmappable.json")
        self.exclusions = self._read("exclusions.json")
        try:
            self.recommendations = self._read("recommendations.json")
        except ConfigError:
            self.recommendations = {"parameter_actions": {}, "cohort_actions": {}, "general": []}
        try:
            self.scoring = self._read("scoring.json")
        except ConfigError:
            self.scoring = {}

    # ---------- indexing ----------

    def _index(self):
        self.param_by_id = {p["id"]: p for p in self.parameters}
        self.disease_by_name = {d["name"]: d for d in self.diseases}
        self.cohort_by_id = {c["id"]: c for c in self.cohorts}

        # alias -> parameter id. The first parameter to claim a key keeps it, and every
        # later claim by a DIFFERENT parameter is recorded as a collision for validate()
        # to report. The previous rule compared the alias's length against the length of
        # the parameter ID it was already mapped to - two unrelated strings - so which
        # test won a shared name was effectively arbitrary.
        self.alias_index = {}
        self.alias_collisions = {}
        canon_claims = {}
        for p in self.parameters:
            keys = [p["name"], p["id"].replace("_", " ")] + list(p.get("aliases", []))
            for k in keys:
                nk = norm_key(k)
                if not nk:
                    continue
                prev = self.alias_index.get(nk)
                if prev is None:
                    self.alias_index[nk] = p["id"]
                elif prev != p["id"]:
                    self.alias_collisions.setdefault(nk, {prev}).add(p["id"])
                for ck in canon_keys(nk):
                    if ck:
                        canon_claims.setdefault(ck, set()).add(p["id"])

        # Fallback index over folded names. A folded key that more than one parameter
        # can reach is AMBIGUOUS and is left out, so the fallback refuses rather than
        # guesses.
        self.canon_index = {k: next(iter(v)) for k, v in canon_claims.items() if len(v) == 1}
        self.canon_ambiguous = {k: sorted(v) for k, v in canon_claims.items() if len(v) > 1}

        # disease name -> cohort links, for reverse lookup during scoring
        self.links_by_disease = {}
        for c in self.cohorts:
            for link in c.get("diseases", []):
                self.links_by_disease.setdefault(link["name"], []).append((c, link))

        self.qual_positive = {norm_key(v) for v in self.qual_vocab["positive"]}
        self.qual_negative = {norm_key(v) for v in self.qual_vocab["negative"]}
        self.qual_indeterminate = {norm_key(v) for v in self.qual_vocab["indeterminate"]}
        # keep the raw symbol forms too, which norm_key would strip
        self.qual_positive_raw = {v.strip().lower() for v in self.qual_vocab["positive"]}
        self.qual_negative_raw = {v.strip().lower() for v in self.qual_vocab["negative"]}

    # ---------- validation ----------

    @staticmethod
    def _midrange(pdef):
        ref = pdef.get("ref") or {}
        interval = ref.get("default") or ref.get("male") or ref.get("female")
        if not interval:
            return None
        return (interval[0] + interval[1]) / 2.0

    def _fires_on_normal(self, ref_entry):
        """Would a mid-range, entirely normal result satisfy this condition?

        Conditions that assert normality ('PT is normal', 'INR <= 1.2') are legitimate
        corroborating signals, but they must never be able to fire a cluster by
        themselves - otherwise a completely healthy panel raises a disease.
        """
        pdef = self.param_by_id.get(ref_entry.get("parameter"))
        cond = ref_entry.get("condition") or {}
        if pdef is None:
            return False
        if pdef["type"] != "numeric":
            # A qualitative test is normal when negative.
            if "status" in cond:
                return cond["status"] == "negative"
            return cond.get("abnormal") == "none"
        if cond.get("abnormal") == "none":
            return True
        if cond.get("abnormal") in ("high", "low", "any"):
            return False
        value = self._midrange(pdef)
        if value is None:
            return False
        if "between" in cond:
            return cond["between"][0] <= value <= cond["between"][1]
        if "outside" in cond:
            return value < cond["outside"][0] or value > cond["outside"][1]
        for op, test in (("gte", lambda a, b: a >= b), ("gt", lambda a, b: a > b),
                         ("lte", lambda a, b: a <= b), ("lt", lambda a, b: a < b),
                         ("eq", lambda a, b: a == b)):
            if op in cond:
                return test(value, cond[op])
        return False

    def _check_normal_panel_cannot_fire(self, cohort, errors):
        cid = cohort["id"]
        if cohort.get("mode") == "count_of":
            met = 0
            for comp in cohort.get("components", []):
                if any(self._fires_on_normal(r) for r in comp.get("any_of", [])):
                    met += 1
            if met >= cohort.get("count_required", 1):
                errors.append(
                    "%s: %d of its components are satisfied by entirely normal values, "
                    "which meets count_required=%d - this cluster would fire on a healthy "
                    "panel" % (cid, met, cohort["count_required"]))
            return
        normal_triggers = [r["parameter"] for r in cohort.get("triggers", [])
                           if self._fires_on_normal(r)]
        if len(normal_triggers) >= cohort.get("min_triggers", 1):
            errors.append(
                "%s: triggers %s are satisfied by normal values and alone meet "
                "min_triggers=%d - this cluster would fire on a healthy panel. Move "
                "normality conditions to 'supporting'."
                % (cid, normal_triggers, cohort.get("min_triggers", 1)))

    def validate(self):
        errors, warnings = [], []
        pids = set(self.param_by_id)
        dnames = set(self.disease_by_name)

        seen_cohorts = set()
        for c in self.cohorts:
            cid = c.get("id")
            if not cid:
                errors.append("cohort with no id in %s" % c.get("_source_file"))
                continue
            if cid in seen_cohorts:
                errors.append("duplicate cohort id: %s" % cid)
            seen_cohorts.add(cid)

            mode = c.get("mode", "weighted")
            if mode == "count_of":
                if not c.get("components"):
                    errors.append("%s: mode count_of but no components" % cid)
                if not isinstance(c.get("count_required"), int):
                    errors.append("%s: mode count_of but no integer count_required" % cid)
            elif not c.get("triggers"):
                errors.append("%s: weighted cohort with no triggers" % cid)

            refs = list(c.get("triggers", [])) + list(c.get("supporting", []))
            for comp in c.get("components", []):
                refs += comp.get("any_of", [])
            for r in refs:
                if r.get("parameter") not in pids:
                    errors.append("%s: unknown parameter '%s'" % (cid, r.get("parameter")))
                if not r.get("condition"):
                    errors.append("%s: reference to '%s' has no condition" % (cid, r.get("parameter")))
            for ep in c.get("expected_parameters", []):
                if ep not in pids:
                    errors.append("%s: unknown expected_parameter '%s'" % (cid, ep))

            self._check_normal_panel_cannot_fire(c, errors)

            if not c.get("diseases"):
                warnings.append("%s: cohort maps to no disease" % cid)
            for link in c.get("diseases", []):
                if link["name"] not in dnames:
                    errors.append("%s: disease '%s' is not in the Disease Master" % (cid, link["name"]))
                w = link.get("weight")
                if not isinstance(w, (int, float)) or not 0 < w <= 1:
                    errors.append("%s -> %s: weight must be in (0,1]" % (cid, link["name"]))
                # Every link must name the Disease Master field that justifies it.
                # This is the audit trail from a lab value to a reported condition, so a
                # missing basis is a defect, not a style issue.
                if not link.get("dm_basis"):
                    errors.append("%s -> %s: no dm_basis recorded - every disease link must "
                                  "quote the Disease Master field that justifies it"
                                  % (cid, link["name"]))
                # A confounder penalty silently removes a condition from the report, so
                # it has to say which Disease Master criterion it is enforcing AND admit
                # that its damping floor is a rule-design number rather than a clinical
                # coefficient. Undocumented damping is an unexplainable rule.
                spec = link.get("requires_support")
                if spec:
                    if not spec.get("basis"):
                        errors.append("%s -> %s: requires_support has no basis - it must "
                                      "quote the criterion it enforces" % (cid, link["name"]))
                    if not spec.get("weight_source"):
                        errors.append("%s -> %s: requires_support has no weight_source - the "
                                      "damping floor must be declared as a design value or "
                                      "sourced" % (cid, link["name"]))
                    floor = spec.get("penalty_when_all_normal", 0.25)
                    if not isinstance(floor, (int, float)) or not 0 < floor <= 1:
                        errors.append("%s -> %s: penalty_when_all_normal must be in (0,1]"
                                      % (cid, link["name"]))
                    for sp in spec.get("parameters", []):
                        if sp not in pids:
                            errors.append("%s -> %s: requires_support names unknown "
                                          "parameter '%s'" % (cid, link["name"], sp))

                for req in link.get("requires_any", []):
                    if req not in pids:
                        errors.append("%s -> %s: requires_any names unknown parameter '%s'"
                                      % (cid, link["name"], req))
                    elif req not in self._all_referenced(c):
                        errors.append("%s -> %s: requires_any names '%s', which the cohort "
                                      "never evaluates" % (cid, link["name"], req))
            # A cohort asserts a clinical relationship, so it must cite the literature or
            # guideline that establishes it. Uncited cohorts are rejected outright.
            if not c.get("evidence"):
                errors.append("%s: no evidence citations - every cohort must cite the "
                              "clinical reference establishing its pattern" % cid)
            else:
                for e in c["evidence"]:
                    if not e.get("citation") or not e.get("note"):
                        errors.append("%s: an evidence entry is missing its citation or note"
                                      % cid)

        for nk, pids in sorted(self.alias_collisions.items()):
            errors.append("alias '%s' is claimed by more than one parameter: %s - a report "
                          "name must map to exactly one test" % (nk, ", ".join(sorted(pids))))
        for p in self.parameters:
            sub = p.get("stands_in_for")
            if sub:
                if sub.get("parameter") not in pids:
                    errors.append("parameter %s: stands_in_for names unknown '%s'"
                                  % (p["id"], sub.get("parameter")))
                if not sub.get("basis"):
                    errors.append("parameter %s: stands_in_for has no documented basis"
                                  % p["id"])
        for c in self.cohorts:
            for link in c.get("diseases", []):
                specs = link.get("direct_evidence") or []
                if isinstance(specs, dict):
                    specs = [specs]
                for spec in specs:
                    if spec.get("parameter") not in pids:
                        errors.append("%s -> %s: direct_evidence names unknown parameter '%s'"
                                      % (c.get("id"), link["name"], spec.get("parameter")))
                    if not spec.get("source"):
                        errors.append("%s -> %s: direct_evidence has no source - a single "
                                      "measurement may only establish a finding where the "
                                      "basis is recorded" % (c.get("id"), link["name"]))

        for p in self.parameters:
            if p["type"] == "numeric" and "ref" not in p and "bands_by_sex" not in p:
                warnings.append("parameter %s: numeric with no reference interval" % p["id"])
            for src in p.get("derived_from", {}).get("inputs", []):
                if src not in pids:
                    errors.append("parameter %s: derived from unknown '%s'" % (p["id"], src))

        expected_unmapped = {u["name"] for u in self.unmappable.get("conditions", [])}
        linked = set(self.links_by_disease)
        unlinked = dnames - linked - expected_unmapped
        for n in sorted(unlinked):
            warnings.append("Disease Master row '%s' has no cohort mapping" % n)

        return {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "counts": {
                "diseases": len(self.diseases),
                "parameters": len(self.parameters),
                "aliases": len(self.alias_index),
                "cohorts": len(self.cohorts),
                "disease_links": sum(len(v) for v in self.links_by_disease.values()),
                "diseases_linked": len(linked),
                "diseases_intentionally_unmapped": len(expected_unmapped),
            },
        }

    @staticmethod
    def _all_referenced(cohort):
        """Every parameter this cohort can evaluate, across triggers, supporting and components."""
        out = set()
        for r in cohort.get("triggers", []) + cohort.get("supporting", []):
            out.add(r.get("parameter"))
        for comp in cohort.get("components", []):
            for r in comp.get("any_of", []):
                out.add(r.get("parameter"))
        return out

    # ---------- helpers ----------

    def resolve_alias(self, raw_name):
        """Map a report/JSON test name onto a canonical parameter id, or None.

        Memoised: the row parser asks about the same candidate names many times while
        choosing where a result's name ends, and the dictionary does not change once
        loaded.
        """
        cache = self.__dict__.setdefault("_resolve_cache", {})
        key = str(raw_name)
        if key not in cache:
            if len(cache) > 20000:
                cache.clear()
            cache[key] = self._resolve_alias(raw_name)
        return cache[key]

    def _resolve_alias(self, raw_name):
        nk = norm_key(raw_name)
        if not nk:
            return None
        if nk in self.alias_index:
            return self.alias_index[nk]
        # try progressively trimmed variants: drop trailing qualifiers in brackets,
        # then drop trailing words, so 'HbA1c (HPLC method)' still resolves.
        stripped = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", str(raw_name))
        # An UNCLOSED bracket is a qualifier cut off by line wrapping or by a truncated
        # field - "PSA (Prostate-Specific Antigen", "HbA1c (Glycosylated". It is dropped
        # the same way a closed one is, which is what lets a short name like "PSA" resolve
        # without the word-trimming fallback (which may not shorten below 4 characters).
        stripped = re.sub(r"\s*[\(\[][^\)\]]*$", " ", stripped)
        nk2 = norm_key(stripped)
        if nk2 and nk2 in self.alias_index:
            return self.alias_index[nk2]

        # Naming variants of a known test: "HsCRP", "HIGHLY SENSITIVE C-REACTIVE
        # PROTEIN", "C-Reactive Protein, High Sensitivity". Tried on the whole name first
        # and the bracket-stripped name second; both are whole-name matches, so a ratio
        # can only land here on its own alias, never on one of its analytes.
        for key in (nk, nk2):
            compact, bag = canon_keys(key)
            for ck in (compact, bag):
                if ck and ck in self.canon_index:
                    return self.canon_index[ck]

        # The text INSIDE the brackets is just as often the recognisable name, and
        # only the outside was ever tried. 'HsCRP (High Sensitivity CRP)' reduces to
        # 'hscrp', which matches nothing, while the parenthetical spells out an alias
        # the dictionary already holds; 'RhD factor (Rh typing)' is the same shape.
        # Both were being dropped as unmapped.
        for inner in re.findall(r"[\(\[]([^\)\]]+)[\)\]]", str(raw_name)):
            ik = norm_key(inner)
            if ik and ik in self.alias_index:
                return self.alias_index[ik]
        # A ratio or index is its OWN quantity, never one of the analytes in its name.
        # Both fallbacks below would otherwise mis-file it: splitting
        # 'Apolipoprotein B/A1 Ratio' on the slash matched 'Apolipoprotein B', so the
        # ratio 1.23 was stored as an ApoB of 1.23 mg/dL and the real ApoB of 142 was
        # lost. 'Albumin/Globulin Ratio' landed on Albumin the same way. If a ratio has
        # no alias of its own, returning None is correct - it is reported as unmapped
        # rather than silently corrupting another parameter.
        # "Total Cholesterol:HDL" is a ratio written with a colon; trimming it back to
        # "Total Cholesterol" filed a ratio of 3.0 as a cholesterol of 3.0 mg/dL.
        if re.search(r"\b(ratio|index)\b|[A-Za-z)]\s*:\s*[A-Za-z(]", str(raw_name), re.I):
            return None

        # 'SGOT/AST' and 'SGPT (ALT)' style dual naming: try each side of the slash.
        for part in re.split(r"[/|]", str(raw_name)):
            pk = norm_key(part)
            # A one- or two-letter side is almost always a unit or a fragment ("K/uL",
            # "mg/dL"), and "k" is potassium's alias: "K/uL Increased by 2.5" became a
            # potassium of 2.5.
            if pk and len(pk) >= 3 and pk in self.alias_index:
                return self.alias_index[pk]

        # Trim trailing qualifier words: 'HbA1c HPLC method' -> 'HbA1c'.
        words = nk2.split() if nk2 else nk.split()
        while len(words) > 1:
            words = words[:-1]
            cand = " ".join(words)
            # Trimming may not shorten a name down to a bare abbreviation: "Hb
            # Electrophoresis" is not haemoglobin and "K increased by" is not potassium.
            # A name that IS the abbreviation still resolves by the exact lookup above.
            if len(cand) >= 4 and cand in self.alias_index:
                return self.alias_index[cand]

        # Last resort: singular/plural. Labs write 'Total Leucocytes Count' where the
        # dictionary holds 'total leucocyte count', which silently dropped the white
        # cell count off a CBC. Only reached once every exact form has missed, so it
        # can add a match but never redirect one that already resolved.
        for base in (nk2 or nk, nk):
            singular = " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss")
                                else w for w in base.split())
            if singular != base and singular in self.alias_index:
                return self.alias_index[singular]
        return None


_CACHE = {}


def get_config(config_dir=None, reload=False):
    key = str(config_dir or CONFIG_DIR)
    if reload or key not in _CACHE:
        _CACHE[key] = Config(config_dir)
    return _CACHE[key]
