#!/usr/bin/env python3
"""
Workflow-run usage attribution — the ONE shared post-processing pass that turns
a session's own log into per-workflow-run cost.

Session-grain usage answers "what did this session cost". It cannot answer "what
did this workflow run cost", which is the question a delivery team actually
asks. This module answers it by segmenting the session's parsed entry stream at
skill/workflow invocation boundaries the logs already record.

Division of labour (deliberate — one algorithm, N hosts):

  * THIS module owns the algorithm: segment, fold usage into the open segment,
    aggregate per run and per workflow, and prove the result reconciles.
  * Each COLLECTOR owns a boundary-detection adapter: it walks its own host's
    parsed entries and emits the ordered event stream below. Only the adapter
    knows what an invocation looks like in that host's logs, and only it knows
    which stretches of wall clock were active. Adding a host = adding an
    adapter; this file is untouched.

The event stream (ordered; the SEGMENTATION is positional, never time-based):

    boundary(workflow, source)   opens a workflow-run segment. `workflow=None`
                                 closes the open segment back to ad-hoc.
    complete(workflow, source)   closes the open segment IF it belongs to that
                                 workflow — the flow declared itself finished.
    usage(tokens, model, cost)   token spend at this position; attributes to
                                 whichever segment is open. `agent=True` marks
                                 a delegate's spend, which becomes its own
                                 detail row under that run.
    elapsed(seconds)             active wall-clock time at this position, folded
                                 into the open segment as `active_s`. Which
                                 seconds count is the adapter's judgement (human
                                 waiting never does); this module only adds them.
    produced(lines, ext)         file content written at this position, folded
                                 into the open segment as `lines` and split by
                                 extension. GROSS lines added, never net: a file
                                 edited twenty times contributes twenty times.
                                 It measures editing activity, not artifact size,
                                 and no surface may present it as the latter.
    interrupt()                  the user stopped the turn here. Marks the open
                                 run rather than closing it — an interrupted run
                                 still spent what it spent, and the flag says the
                                 run is not a clean sample of that workflow.

Semantics, and why:

  * A boundary OPENS a segment; every usage event after it attributes to that
    segment until the segment CLOSES. Repeated invocations of the same skill each
    open their OWN segment, so two runs of one skill are two runs — never
    merged. Interleaved workflows separate for the same reason.
  * A segment closes exactly two ways: the flow declares itself done (a
    `complete` event, from a governed workflow's own terminal artifact), or the
    next boundary opens. There is deliberately NO idle or no-touch rule: a run
    left open because the user stopped for the night and resumed in the morning
    is still that workflow's spend, and a timeout would misattribute precisely
    that case.
  * Spend with no open segment is AD-HOC, and ad-hoc is positional too: each
    stretch between runs is its own segment at its own place in the order. Many
    ad-hoc rows in a session is the normal, correct shape — merging them would
    throw away the one thing that makes them readable.
  * Segmentation is by POSITION in the log, so a session spanning days behaves
    exactly like one spanning minutes. No timestamp is consulted here at all.
  * Deterministic: same events in, same attribution out. No model inference, no
    heuristic ranking, no randomness — the aggregate ordering is a total order.

HARD INVARIANT: sum(runs) + unattributed == totals, token-for-token and
cost-for-cost. `attribute()` checks this itself and raises ReconciliationError
on a mismatch — the check lives in the code path, not only in the tests, because
a number that silently fails to add up is worse than no number.
"""

import math

import ir as IR

# Cost is a float sum, so exact equality is the wrong test: adding per-event
# costs and pricing the aggregate differ in the last bits. Everything else
# (tokens) is integer and compared exactly.
_COST_REL_TOL = 1e-9
_COST_ABS_TOL = 1e-9


class ReconciliationError(AssertionError):
    """sum(runs) + unattributed did not equal the session totals."""


# ── Event constructors (the adapter-facing API) ──────────────────────────

def boundary(workflow, source="", kind="other"):
    """A workflow-run boundary at this position in the log. `workflow=None`
    closes the open segment (usage after it is unattributed again) — used for
    spend that provably belongs to no invocation.

    `kind` is "kit" for a skill the kit declares in agents.yaml and "other" for
    anything else; the adapter decides, since only it knows the host's naming.
    It rides on the boundary because it is a property of the name, constant for
    every boundary that names the same workflow."""
    return {"t": "boundary", "workflow": normalize(workflow), "source": source,
            "kind": kind}


def complete(workflow, source="run-complete"):
    """The open run of `workflow` has reached its END STATE and closes here.

    Scoped to a named workflow on purpose: a completion artifact belongs to one
    skill's cue sheet, so it may only close a run of THAT skill. A terminal
    artifact for some other workflow appearing mid-run means nothing here.

    After a close, spend belongs to ad-hoc chat until the next boundary — which
    is the whole point: a finished authoring run must stop absorbing the
    engineering that happens after it."""
    return {"t": "complete", "workflow": normalize(workflow), "source": source}


def elapsed(seconds, model=None):
    """Wall-clock time at this position, attributed like spend: to whichever
    segment is open. The adapter decides what counts — see the Active definition
    in each collector — and this module only adds it up.

    `model` names the assistant turn that owns this interval, so the run's
    active time can be PARTITIONED across the models that did the work. Main
    rows therefore SUM to the run's Active — they divide one timeline. Agent
    rows do not, because agents run in parallel on their own timelines. Those
    are genuinely different quantities sharing a column, which is why the
    header says "Active / Agent time*" and the footnote spells out both halves.

    Deliberately a separate event kind rather than a field on `usage`: elapsed
    time and token spend do not occur at the same positions (a long tool call
    burns minutes and no tokens), and the session total is not reconciled
    against durations."""
    return {"t": "elapsed", "seconds": float(seconds), "model": model}


def produced(lines, ext=None, model=None):
    """File content written at this position.

    Extension is carried raw and unclassified on purpose: what counts as "code"
    versus a document is a consumer's taxonomy and changes with the audience,
    while the extension is a fact about the file. Classifying here would freeze
    one reading of it into the shared payload.

    MAIN-THREAD writes only — `model` names the detail row they belong to, so
    the split survives a run that switched models. A delegate's lines ride on
    its own usage event instead, because one spawn can cover a whole subtree of
    agents and ordering alone could not say which of them wrote what."""
    return {"t": "produced", "lines": int(lines or 0),
            "ext": (ext or "").lower() or None, "model": model}


def interrupt():
    """The user stopped the turn here.

    Deliberately does NOT close the run. The spend already happened and still
    belongs to that workflow; what changes is that the run is no longer a clean
    observation of it, which is a property a consumer may want to filter on and
    is not the same thing as the run being over."""
    return {"t": "interrupt"}


def usage(tokens, model=None, cost=None, label=None, desc=None, turns=None,
          agent=False, duration_s=None, agent_id=None, role=None,
          lines=0, lines_by_ext=None):
    """Token spend at this position. `tokens` is an IR Tokens dict — an adapter
    holding a raw host usage dict folds it with IR.add_tokens first, so this
    module never has to know a host's key names. `cost` is the adapter's own
    priced value (None when the model is unknown and cost cannot be computed).

    `agent=True` marks spend by a delegated agent: it becomes its own detail row
    under the run, carrying `label` / `desc` / `turns` for display. Main-thread
    spend (the default) collapses into a single "this run's share" detail row per
    run, because a run can hold hundreds of main-thread entries and nobody wants
    to read them one by one.

    `duration_s` on an agent event is that agent's OWN span — informational, and
    never added into the run's active time (see elapsed())."""
    return {"t": "usage", "tokens": dict(tokens), "model": model, "cost": cost,
            "label": label, "desc": desc, "turns": turns, "agent": bool(agent),
            "duration_s": duration_s, "agent_id": agent_id, "role": role,
            "lines": int(lines or 0), "lines_by_ext": dict(lines_by_ext or {})}


def normalize(workflow):
    """Canonical workflow name, so the same skill aggregates as one workflow no
    matter which marker announced it. Strips an invocation slash and a plugin
    namespace: `/adk:adk-implement-pbi` and `adk-implement-pbi` are one
    workflow. None passes through (it means "close the segment")."""
    if workflow is None:
        return None
    name = str(workflow).strip()
    if name.startswith("/"):
        name = name[1:]
    if ":" in name:
        name = name.rsplit(":", 1)[1]
    return name.strip()


# ── The shared algorithm ─────────────────────────────────────────────────

def _blank(**extra):
    """A fresh accumulator row. Cost starts at None, not 0.0: "nothing priced
    here" and "priced at zero" are different answers, and only the first is
    honest when a host records no model.

    `_mains` and `_agents` are working state for the detail rows and are dropped
    before the row is published (see _publish)."""
    row = {"tokens": IR.empty_tokens(), "cost_usd": None, "events": 0,
           "active_s": None, "lines": 0, "lines_by_ext": {},
           "interrupted": False, "_mains": {}, "_agents": []}
    row.update(extra)
    return row


def _detail(kind, event):
    return {"kind": kind, "label": event.get("label"), "desc": event.get("desc"),
            # The kit ROLE this delegate was given, when it could be resolved
            # from the registry — never a guess, and None for a generic agent.
            "role": event.get("role"),
            "lines": 0, "lines_by_ext": {},
            "model": event.get("model"), "turns": event.get("turns"),
            # An agent's own span. Never summed with its siblings' — they ran in
            # parallel — and never added to the run's active_s.
            "duration_s": event.get("duration_s"),
            # The agent's own id, so a surface that also renders the transcript
            # can link a detail row to that agent's card.
            "agent_id": event.get("agent_id"),
            "tokens": IR.empty_tokens(), "cost_usd": None, "events": 0}


def _fold(row, event):
    IR.add_tokens_from(row["tokens"], event["tokens"])
    row["events"] += 1
    cost = event.get("cost")
    if cost is not None:
        row["cost_usd"] = (row["cost_usd"] or 0.0) + cost
    # …and again into the detail row this event belongs to. A delegated agent
    # gets its own row; main-thread spend accumulates into one row PER MODEL.
    #
    # Per model, not one row overall: a user switches model mid-conversation, and
    # a single row listing two models could not be recomputed from one rate card
    # — the same objection that makes a mixed-rate master row only checkable one
    # level down. Splitting here keeps every detail row checkable at the model it
    # names. The model already sits on the row's second line, so two rows both
    # reading "Main conversation" are self-explanatory.
    if event.get("agent"):
        target = _detail("agent", event)
        row["_agents"].append(target)
        # A delegate's lines ride on its own usage event, so they reach both the
        # segment total and that delegate's row without depending on emit order.
        _fold_lines(row, target, event.get("lines") or 0,
                    event.get("lines_by_ext") or {})
    else:
        target = _main_row(row, event.get("model"))
    IR.add_tokens_from(target["tokens"], event["tokens"])
    target["events"] += 1
    if cost is not None:
        target["cost_usd"] = (target["cost_usd"] or 0.0) + cost


def _main_row(row, model):
    """Get or create the main-conversation detail row for `model`.

    One row per model, because a run that switched models has one recomputable
    line per rate card. Reachable from either a usage event or an elapsed one:
    time and tokens arrive at different positions, and a model that did work
    must appear whichever of the two lands first."""
    target = row["_mains"].get(model)
    if target is None:
        target = row["_mains"][model] = {
            "kind": "main", "label": None, "desc": None, "model": model,
            "turns": None, "duration_s": None, "agent_id": None, "role": None,
            "lines": 0, "lines_by_ext": {},
            "tokens": IR.empty_tokens(), "cost_usd": None, "events": 0}
    return target


def _fold_elapsed(row, seconds, model=None):
    """Active time for the open segment, and its share for the model that did
    the work. Adapters emit only intervals they have already ruled active, and
    those intervals never overlap, so adding them IS the union.

    INVARIANT: because every interval is charged to exactly one model AND to the
    segment, the main rows' durations sum to the segment's `active_s` exactly."""
    row["active_s"] = (row["active_s"] or 0.0) + seconds
    if model is not None:
        target = _main_row(row, model)
        target["duration_s"] = (target["duration_s"] or 0.0) + seconds


def _fold_lines(row, target, lines, by_ext):
    """Add lines to a segment AND to the detail row that produced them.

    One function so the two can never disagree: a detail breakdown that did not
    add up to the row above it would be exactly the defect the token
    reconciliation already guards against."""
    if not lines and not by_ext:
        return
    row["lines"] += lines
    target["lines"] += lines
    for ext, n in (by_ext or {}).items():
        row["lines_by_ext"][ext] = row["lines_by_ext"].get(ext, 0) + n
        target["lines_by_ext"][ext] = target["lines_by_ext"].get(ext, 0) + n


def _fold_produced(row, lines, ext, model):
    """Main-thread lines: to the segment, and to the main row for the model that
    wrote them, so a run that switched models keeps the split."""
    _fold_lines(row, _main_row(row, model), lines,
                {ext: lines} if ext else {})


def _add_row(acc, row):
    IR.add_tokens_from(acc["tokens"], row["tokens"])
    acc["events"] += row["events"]
    acc["lines"] += row["lines"]
    for ext, n in row["lines_by_ext"].items():
        acc["lines_by_ext"][ext] = acc["lines_by_ext"].get(ext, 0) + n
    acc["interrupted"] = acc["interrupted"] or row["interrupted"]
    if row["cost_usd"] is not None:
        acc["cost_usd"] = (acc["cost_usd"] or 0.0) + row["cost_usd"]
    # Active time DOES add from runs to their workflow: the runs of one workflow
    # are disjoint stretches of the same single main thread. (Agent times do not
    # add to anything — see elapsed().)
    if row["active_s"] is not None:
        acc["active_s"] = (acc["active_s"] or 0.0) + row["active_s"]


def _headline(row):
    row["headline_tokens"] = IR.headline_tokens(row["tokens"])
    return row


def _detail_rows(row, run_no=None):
    """This row's detail, main share first then each delegated agent, in the
    order they appeared. `run_no` stamps which run of the workflow each row
    belongs to, so a multi-run workflow's detail stays readable."""
    rows = list(row["_mains"].values()) + row["_agents"]
    out = []
    for detail in rows:
        published = _headline(dict(detail))
        if run_no is not None:
            published["run"] = run_no
        out.append(published)
    return out


def _publish(row, detail):
    """Turn an accumulator into the published row: drop the working state, add
    the headline figure, the detail list, and the delegate count the surfaces
    show in a row's meta line (and use to decide whether the row expands at
    all — a workflow with no delegates has nothing behind it)."""
    published = {k: v for k, v in row.items() if not k.startswith("_")}
    published["detail"] = detail
    published["agents"] = sum(1 for d in detail if d.get("kind") == "agent")
    return _headline(published)


def attribute(events, session_tokens=None, session_cost=None):
    """Segment `events` into workflow runs and aggregate.

    Returns:
      {"segments":      [ Segment, ... ],      # WHAT THE TABLE RENDERS, in order
       "runs":          [ Segment, ... ],      # the workflow-run subset
       "unattributed":  [ Segment, ... ],      # the ad-hoc spans, positioned
       "workflows":     [ WorkflowAgg, ... ],  # per-skill rollup, NOT the table
       "totals":        Bucket,
       "reconciled":    True}

    Every row carries `tokens`, `cost_usd`, `headline_tokens`, `events`,
    `active_s`, `agents` and `detail`; runs and workflows also carry `kind`
    ("kit"/"other") and `order` — the event position of first activity, which is
    how a surface renders the table CHRONOLOGICALLY while the composition bar
    stays sorted by size.

    `session_tokens` / `session_cost`, when given, are the collector's own
    session totals: the result is verified against them too, so the invariant is
    checked end-to-end and not merely against this function's own bookkeeping.
    Durations are NOT reconciled and never will be — see elapsed().

    Raises ReconciliationError when the parts do not add up to the whole.
    """
    segments = []      # every segment, in the order it opened — runs AND ad-hoc
    open_run = None    # the workflow run currently accruing spend, if any
    open_adhoc = None  # the ad-hoc stretch currently accruing spend, if any
    counts = {}

    # `position` advances on boundary and usage events only — never on elapsed.
    # It is what the surfaces sort the table by, so it must not shift because a
    # host happened to record more clock ticks: ordering is positional, like the
    # segmentation itself.
    #
    # It also advances whenever a segment OPENS, and that is not a detail:
    # `order` is the row KEY both surfaces fold and patch rows by, so two
    # segments sharing one is corruption, not a cosmetic tie. Making every
    # segment consume exactly one position is what makes duplicates impossible —
    # the invariant is structural rather than something a caller must remember.
    position = 0

    def target_for():
        """The segment that owns spend at this position, opening a fresh ad-hoc
        stretch when no run is running. Ad-hoc is a SEGMENT, not a bucket: the
        stretch between two runs is its own row at its own place in the order."""
        nonlocal open_adhoc, position
        if open_run is not None:
            return open_run
        if open_adhoc is None:
            open_adhoc = _blank(kind="adhoc", workflow=None, order=position,
                                source="", closed_by=None)
            segments.append(open_adhoc)
            # Claim the position this segment opened at. Without this, an ad-hoc
            # stretch opened by an elapsed/produced/interrupt event — none of
            # which advance `position` — handed the SAME order to the next
            # boundary's run, and the two rows then shared a key.
            position += 1
        return open_adhoc

    for event in events:
        kind = event["t"]
        if kind == "boundary":
            name = event["workflow"]
            # Any boundary ends whatever was accruing — a run because the next
            # invocation supersedes it, an ad-hoc stretch because it is over.
            if open_run is not None and open_run["closed_by"] is None:
                open_run["closed_by"] = "boundary"
            open_run, open_adhoc = None, None
            if name is None:
                continue                      # an explicit close, nothing opens
            counts[name] = counts.get(name, 0) + 1
            open_run = _blank(seq=sum(1 for x in segments
                                      if x["kind"] != "adhoc") + 1,
                              workflow=name, run=counts[name],
                              source=event.get("source", ""),
                              kind=event.get("kind", "other"),
                              closed_by=None,
                              # `order` is where this segment opened in the
                              # stream. The surfaces sort the table by it,
                              # because a reader follows a session forwards.
                              order=position)
            segments.append(open_run)
            position += 1
        elif kind == "complete":
            # The flow declared itself finished. Only its OWN completion closes
            # it — another workflow's terminal artifact is not evidence here.
            if open_run is not None and open_run["workflow"] == event["workflow"]:
                open_run["closed_by"] = event.get("source") or "run-complete"
                open_run, open_adhoc = None, None
        elif kind == "usage":
            _fold(target_for(), event)
            position += 1
        elif kind == "elapsed":
            _fold_elapsed(target_for(), event["seconds"], event.get("model"))
        elif kind == "produced":
            _fold_produced(target_for(), event["lines"],
                           event.get("ext"), event.get("model"))
        elif kind == "interrupt":
            # Marks the open segment — including an ad-hoc stretch, since a
            # stopped turn outside a run is still a stopped turn.
            target_for()["interrupted"] = True

    runs = [x for x in segments if x["kind"] != "adhoc"]
    adhoc = [x for x in segments if x["kind"] == "adhoc"]

    # Per-workflow aggregate. Built by first-seen order, then sorted into a
    # total order so the same session always renders the same table. Each
    # workflow's detail is its runs' detail, stamped with the run it came from.
    by_workflow, detail_by_workflow = {}, {}
    for run in runs:
        agg = by_workflow.get(run["workflow"])
        if agg is None:
            agg = by_workflow[run["workflow"]] = _blank(
                workflow=run["workflow"], runs=0, kind=run["kind"],
                order=run["order"])   # first activity — the chronological key
            detail_by_workflow[run["workflow"]] = []
        agg["runs"] += 1
        _add_row(agg, run)
        detail_by_workflow[run["workflow"]].extend(_detail_rows(run, run["run"]))
    workflows = sorted(by_workflow.values(),
                       key=lambda w: (-(w["cost_usd"] or 0.0),
                                      -IR.headline_tokens(w["tokens"]),
                                      w["workflow"]))

    totals = _blank()
    for segment in segments:
        _add_row(totals, segment)

    published = {id(x): _publish(x, _detail_rows(x)) for x in segments}
    result = {
        # The rendering list: one row per invocation and per ad-hoc span, in the
        # order they happened. No per-skill rollup — two runs of one skill days
        # apart are two rows, because the chronology IS the story.
        "segments": [published[id(x)] for x in segments],
        "runs": [published[id(x)] for x in runs],
        # A LIST of positioned spans, never one bucket. A single aggregate at
        # order 0 was actively misleading: most of it is post-closure spend from
        # the middle of a session, and summing it drew that as if it happened
        # first. Position is the point.
        "unattributed": [published[id(x)] for x in adhoc],
        # Per-skill rollup. NOT what the table renders — it exists so the top
        # metrics can count DISTINCT skills ("2 kit workflows" is right even
        # when they ran five times).
        "workflows": [_publish(w, detail_by_workflow[w["workflow"]])
                      for w in workflows],
        "totals": _publish(totals, []),
        "reconciled": True,
    }
    _reconcile(result, session_tokens, session_cost)
    return result


def _reconcile(result, session_tokens, session_cost):
    """The hard invariant, enforced in the code path.

    Checks sum(runs) + unattributed against this pass's own totals, and — when
    the collector supplied them — against the session totals the rest of the
    product reports. Either mismatch means a segment leaked or double-counted
    spend, which must be loud rather than quietly wrong."""
    parts = IR.empty_tokens()
    for segment in result["segments"]:
        IR.add_tokens_from(parts, segment["tokens"])
    _same_tokens(parts, result["totals"]["tokens"], "segments", "totals")

    # ...and the runs/unattributed split must be the same whole seen another way.
    split = IR.empty_tokens()
    for row in result["runs"] + result["unattributed"]:
        IR.add_tokens_from(split, row["tokens"])
    _same_tokens(split, result["totals"]["tokens"], "runs + unattributed", "totals")

    # The detail rows are what a reader expands to see, so they have to add up
    # to the master row above them too — a detail that does not sum to its own
    # headline is the same defect as a run that does not sum to the session.
    for row in result["segments"] + result["workflows"]:
        if not row["detail"]:
            continue
        shown = IR.empty_tokens()
        for detail in row["detail"]:
            IR.add_tokens_from(shown, detail["tokens"])
        _same_tokens(shown, row["tokens"],
                     "detail rows of %s" % row.get("workflow", "unattributed"),
                     "their own total")

    # Lines: the detail breakdown a reader expands must add up to the figure on
    # the row above it, for the same reason the tokens must.
    for row in result["segments"]:
        if not row["detail"]:
            continue
        shown = sum(d["lines"] for d in row["detail"])
        if shown != row["lines"]:
            raise ReconciliationError(
                "workflow attribution does not reconcile: detail rows of %s "
                "wrote %d lines but their own total says %d"
                % (row.get("workflow") or "ad-hoc", shown, row["lines"]))

    # Durations: main rows PARTITION the segment's active time, so they sum to
    # it exactly. Agent rows are independent parallel spans and are excluded —
    # this is the check that catches a future "helpful" folding of agent time
    # into the parent.
    for row in result["segments"]:
        shares = [d["duration_s"] for d in row["detail"]
                  if d["kind"] != "agent" and d["duration_s"] is not None]
        if not shares:
            continue
        _same_cost(sum(shares), row["active_s"],
                   "main-conversation shares", "the segment's active time")

    part_cost = _sum_cost([r["cost_usd"] for r in
                           result["runs"] + result["unattributed"]])
    _same_cost(part_cost, result["totals"]["cost_usd"], "runs + unattributed", "totals")

    if session_tokens is not None:
        _same_tokens(result["totals"]["tokens"], session_tokens,
                     "attributed totals", "session tokens")
    if session_cost is not None:
        _same_cost(result["totals"]["cost_usd"], session_cost,
                   "attributed totals", "session cost")


def _sum_cost(values):
    priced = [v for v in values if v is not None]
    return sum(priced) if priced else None


def _same_tokens(left, right, left_label, right_label):
    for field in IR.empty_tokens():
        if left.get(field, 0) != right.get(field, 0):
            raise ReconciliationError(
                "workflow attribution does not reconcile: %s %s=%s but %s %s=%s"
                % (left_label, field, left.get(field, 0),
                   right_label, field, right.get(field, 0)))


def _same_cost(left, right, left_label, right_label):
    if left is None and right is None:
        return
    if left is None or right is None:
        raise ReconciliationError(
            "workflow attribution does not reconcile: %s cost=%s but %s cost=%s"
            % (left_label, left, right_label, right))
    if not math.isclose(left, right, rel_tol=_COST_REL_TOL, abs_tol=_COST_ABS_TOL):
        raise ReconciliationError(
            "workflow attribution does not reconcile: %s cost=%r but %s cost=%r"
            % (left_label, left, right_label, right))


def empty():
    """The attribution result for a session with no events — same shape, so
    consumers never have to special-case a session that invoked nothing."""
    return attribute([])
