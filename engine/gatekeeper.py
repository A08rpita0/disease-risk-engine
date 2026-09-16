"""Stage 3.5 - hard exclusion gates (veto logic).

A definitive negative marker outranks any number of non-specific secondary signals.
Raised ALT, AST and lymphocytes are compatible with a dozen things; an explicitly
non-reactive HBsAg is not compatible with Hepatitis B. So the negative wins outright:
the condition is suppressed, not merely scored lower.

Three states are kept distinct, and only one of them vetoes:

    explicitly negative -> VETO        the test was done and came back negative
    positive / equivocal -> no veto    normal rules evaluate
    missing              -> no veto    unknown is not negative; coverage handles it

That last line is the one that matters most. Treating an absent test as a negative
would silently suppress real findings in sparse reports.

Gates run BEFORE cohort confidence is finalised and before disease scoring, so a
vetoed pattern contributes nothing to any score and produces no recommendation.
Every suppression records its reason for audit.
"""
from __future__ import annotations


class Veto:
    """One active exclusion, with everything needed to explain it."""

    __slots__ = ("rule_id", "parameter", "parameter_name", "observed", "reason",
                 "user_message", "basis", "diseases", "cohorts")

    def __init__(self, rule, parameter_name, observed):
        self.rule_id = rule["id"]
        self.parameter = rule["parameter"]
        self.parameter_name = parameter_name
        self.observed = observed
        self.reason = rule.get("reason") or "definitive negative marker present"
        self.user_message = rule.get("user_message") or ""
        self.basis = rule.get("basis") or ""
        self.diseases = set(rule.get("vetoes_diseases") or [])
        self.cohorts = set(rule.get("vetoes_cohorts") or [])

    def audit(self):
        return {
            "rule_id": self.rule_id,
            "parameter": self.parameter,
            "parameter_name": self.parameter_name,
            "observed": self.observed,
            "reason": "Suppressed by hard exclusion: %s." % self.reason,
            "user_message": self.user_message,
            "basis": self.basis,
            "suppressed_diseases": sorted(self.diseases),
            "suppressed_cohorts": sorted(self.cohorts),
        }

    def __repr__(self):
        return "<Veto %s>" % self.rule_id


def _is_explicit_negative(param, negative_when):
    """True only when the test was actually done and is definitively negative."""
    if param is None:
        return False, None                      # missing stays missing

    # --- qualitative / categorical status ---
    wanted = negative_when.get("status_in")
    if wanted and param.status is not None:
        if param.status in wanted:
            return True, "reported as %s" % param.status
        # An explicit positive or equivocal result must not fall through to the
        # numeric branch and veto by accident.
        return False, None

    # --- numeric cut-off (signal-to-cutoff index, e.g. COI) ---
    below = negative_when.get("numeric_below")
    if below is not None:
        value = param.value if getattr(param, "interpretable", True) else None
        if value is None and getattr(param, "interpretable", True):
            value = getattr(param, "numeric_raw", None)
        if value is not None and value < below:
            return True, "%s below the %s cut-off" % (_fmt(value), _fmt(below))

    return False, None


def _fmt(x):
    if isinstance(x, float):
        return ("%g" % round(x, 4))
    return str(x)


class Gatekeeper:
    """Evaluates the configured exclusion rules against one patient."""

    def __init__(self, config):
        self.cfg = config
        self.rules = [r for r in (config.exclusions.get("rules") or [])
                      if r.get("enabled", True)]

    def evaluate(self, patient):
        """Return the list of Vetoes active for this patient."""
        active = []
        for rule in self.rules:
            param = patient.get(rule["parameter"])
            ok, observed = _is_explicit_negative(param, rule.get("negative_when") or {})
            if not ok:
                continue

            # A rule may require several markers to be negative together. Every one of
            # them must be explicitly negative - if any is missing or positive, no veto.
            # This is what stops a negative NS1 with IgM absent from ruling out dengue.
            companions_ok = True
            for other_id in rule.get("also_negative") or []:
                other = patient.get(other_id)
                ok2, obs2 = _is_explicit_negative(other, rule.get("negative_when") or {})
                if not ok2:
                    companions_ok = False
                    break
                observed = "%s; %s %s" % (observed, other.name, obs2)
            if not companions_ok:
                continue

            active.append(Veto(rule, param.name, observed))
        return active

    # ------------------------------------------------------------------

    @staticmethod
    def disease_vetoed(name, vetoes):
        for v in vetoes:
            if name in v.diseases:
                return v
        return None

    @staticmethod
    def cohort_vetoed(cohort_id, vetoes):
        for v in vetoes:
            if cohort_id in v.cohorts:
                return v
        return None
