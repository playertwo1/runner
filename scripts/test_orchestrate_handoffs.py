import json
import tempfile
import unittest
from pathlib import Path

from scripts.orchestrate_handoffs import (
    HandoffError,
    approve_gate,
    audit_handoff,
    builder_handoff,
    init_state,
    next_actor,
    status,
)


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def dump(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def policy(builder="builder", auditor="auditor", rounds=3):
    return {
        "schema_version": "0.1",
        "id": "builder-auditor-loop",
        "builder_role_id": builder,
        "auditor_role_id": auditor,
        "product_authority_id": "human-owner",
        "max_audit_rounds": rounds,
        "immutable_audit_target": True,
        "auditor_write_access": False,
        "human_gate_required": True,
        "auto_advance_after_audit": False,
        "persist_handoffs": True,
    }


def builder_report(result="READY_FOR_AUDIT", sha=SHA_A):
    return {
        "schema_version": "0.1",
        "executor_id": "builder-executor",
        "role": "BUILDER",
        "authority": "IMPLEMENTATION",
        "result_sha": sha,
        "result": result,
        "summary": "Mudança concluída dentro do escopo autorizado.",
        "changed_paths": ["src/example.txt"],
        "checks": [{"id": "unit", "status": "PASS", "evidence": "suite green"}],
        "limitations": [],
        "disputed_findings": [],
        "escalation": None,
    }


def audit_report(sha: str, result="PASS", blocking=False):
    findings = []
    if blocking:
        findings.append(
            {
                "id": "AUD-001",
                "severity": "HIGH",
                "blocking": True,
                "files": ["src/example.txt"],
                "evidence": "evidence",
                "problem": "blocking problem",
                "violated_criterion": "criterion",
                "resolution_condition": "fix condition",
            }
        )
    return {
        "schema_version": "0.1",
        "executor_id": "auditor-executor",
        "role": "AUDITOR",
        "authority": "INDEPENDENT_AUDIT",
        "audit_result": result,
        "audited_sha": sha,
        "summary": "Independent review complete.",
        "findings": findings,
        "checks": [{"id": "scope", "status": "PASS", "evidence": "reviewed"}],
        "residual_risks": [],
        "gate_registration": "NOT_AUTHORIZED",
    }


class OrchestrateHandoffsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy_path = self.root / "policy.json"
        self.state_path = self.root / "state.json"
        dump(self.policy_path, policy())
        init_state(
            self.policy_path,
            self.state_path,
            project_id="sample",
            phase="F01",
            gate="G01",
            builder_branch="work/sample-f01",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_reference_policy_initializes_exactly_three_audit_rounds(self):
        reference_policy = Path(__file__).resolve().parents[1] / "orchestration" / "builder-auditor-policy.json"
        reference_state = self.root / "reference-state.json"

        state = init_state(
            reference_policy,
            reference_state,
            project_id="sample",
            phase="O0",
            gate="S1",
            builder_branch="builder/o0-c21",
        )

        self.assertEqual(3, state["max_audit_rounds"])

    def test_init_generates_deterministic_unambiguous_run_id(self):
        expected = "run-220fd788b3617af25bd6f932e3329ab82cade4a5f600f21fa385a52c4baaf790"
        second_state = self.root / "same-logical-run.json"

        repeated = init_state(
            self.policy_path,
            second_state,
            project_id="sample",
            phase="F01",
            gate="G01",
            builder_branch="work/sample-f01",
        )

        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(expected, persisted.get("run_id"))
        self.assertEqual(expected, repeated.get("run_id"))

    def test_init_persists_builder_as_next_actor(self):
        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))

        self.assertEqual("BUILDER", persisted.get("next_actor"))

    def test_inconsistent_next_actor_is_rejected_without_mutation(self):
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["next_actor"] = "AUDITOR"
        dump(self.state_path, state)
        before = self.state_path.read_bytes()

        with self.assertRaisesRegex(HandoffError, "next_actor must be BUILDER for READY_FOR_BUILD"):
            status(self.state_path)

        self.assertEqual(before, self.state_path.read_bytes())

    def test_next_actor_is_persisted_across_handoffs_and_reload(self):
        builder_one = self.root / "builder-next-actor-one.json"
        audit_one = self.root / "audit-next-actor-one.json"
        builder_two = self.root / "builder-next-actor-two.json"
        audit_two = self.root / "audit-next-actor-two.json"
        dump(builder_one, builder_report(sha=SHA_A))
        dump(audit_one, audit_report(SHA_A, result="FAIL", blocking=True))
        dump(builder_two, builder_report(sha=SHA_B))
        dump(audit_two, audit_report(SHA_B))

        built_one = builder_handoff(self.state_path, builder_one, SHA_A)
        audited_one = audit_handoff(self.state_path, audit_one)
        built_two = builder_handoff(self.state_path, builder_two, SHA_B)
        audited_two = audit_handoff(self.state_path, audit_two)
        restarted = status(self.state_path)

        self.assertEqual("AUDITOR", built_one.get("next_actor"))
        self.assertEqual("BUILDER", audited_one.get("next_actor"))
        self.assertEqual("AUDITOR", built_two.get("next_actor"))
        self.assertEqual("PRODUCT_AUTHORITY", audited_two.get("next_actor"))
        self.assertEqual("PRODUCT_AUTHORITY", restarted.get("next_actor"))
        self.assertNotIn(restarted.get("next_actor"), {"BUILDER", "AUDITOR"})

    def test_run_id_is_preserved_across_handoffs_and_reload(self):
        initial = json.loads(self.state_path.read_text(encoding="utf-8"))
        run_id = initial.get("run_id")
        self.assertIsNotNone(run_id)
        builder_path = self.root / "builder-run-id.json"
        audit_path = self.root / "audit-run-id.json"
        dump(builder_path, builder_report())
        dump(audit_path, audit_report(SHA_A))

        built = builder_handoff(self.state_path, builder_path, SHA_A)
        audited = audit_handoff(self.state_path, audit_path)
        reloaded = json.loads(self.state_path.read_text(encoding="utf-8"))

        self.assertEqual(run_id, built["run_id"])
        self.assertEqual(run_id, audited["run_id"])
        self.assertEqual(run_id, reloaded["run_id"])

    def test_audit_round_is_persisted_and_increments_once_per_accepted_audit(self):
        builder_one = self.root / "builder-round-one.json"
        audit_one = self.root / "audit-round-one.json"
        builder_two = self.root / "builder-round-two.json"
        rejected_audit = self.root / "audit-wrong-sha.json"
        audit_two = self.root / "audit-round-two.json"
        dump(builder_one, builder_report(sha=SHA_A))
        dump(audit_one, audit_report(SHA_A, result="FAIL", blocking=True))
        dump(builder_two, builder_report(sha=SHA_B))
        dump(rejected_audit, audit_report(SHA_A))
        dump(audit_two, audit_report(SHA_B))

        built_one = builder_handoff(self.state_path, builder_one, SHA_A)
        audited_one = audit_handoff(self.state_path, audit_one)
        restarted = status(self.state_path)
        built_two = builder_handoff(self.state_path, builder_two, SHA_B)

        self.assertEqual(0, built_one["audit_round"])
        self.assertEqual(1, audited_one["audit_round"])
        self.assertEqual(1, restarted["audit_round"])
        self.assertEqual(1, built_two["audit_round"])

        before_rejection = self.state_path.read_bytes()
        with self.assertRaisesRegex(HandoffError, "Audit SHA mismatch"):
            audit_handoff(self.state_path, rejected_audit)
        self.assertEqual(before_rejection, self.state_path.read_bytes())

        audited_two = audit_handoff(self.state_path, audit_two)
        self.assertEqual(2, audited_two["audit_round"])
        before_replay = self.state_path.read_bytes()
        with self.assertRaisesRegex(HandoffError, "Audit handoff not allowed"):
            audit_handoff(self.state_path, audit_two)
        self.assertEqual(before_replay, self.state_path.read_bytes())
        self.assertEqual(2, status(self.state_path)["audit_round"])

    def test_persisted_state_requires_audit_round(self):
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        del state["audit_round"]
        dump(self.state_path, state)
        before = self.state_path.read_bytes()

        with self.assertRaisesRegex(HandoffError, "state schema validation failed"):
            status(self.state_path)

        self.assertEqual(before, self.state_path.read_bytes())

    def test_audit_at_max_round_is_rejected_without_state_change(self):
        builder_path = self.root / "builder-at-max-round.json"
        audit_path = self.root / "audit-at-max-round.json"
        dump(builder_path, builder_report(sha=SHA_A))
        dump(audit_path, audit_report(SHA_A))
        builder_handoff(self.state_path, builder_path, SHA_A)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["machine_state"] = "READY_FOR_AUDIT"
        state["next_actor"] = "AUDITOR"
        state["audit_round"] = 3
        state["max_audit_rounds"] = 3
        dump(self.state_path, state)
        before = self.state_path.read_bytes()

        with self.assertRaisesRegex(HandoffError, "Maximum audit rounds already reached"):
            audit_handoff(self.state_path, audit_path)

        self.assertEqual(before, self.state_path.read_bytes())
        self.assertEqual(3, status(self.state_path)["audit_round"])

    def test_inconsistent_frozen_sha_state_is_rejected_without_mutation(self):
        audit_path = self.root / "audit-inconsistent-state.json"
        dump(audit_path, audit_report(SHA_B))
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["machine_state"] = "READY_FOR_AUDIT"
        state["next_actor"] = "AUDITOR"
        state["builder_head_sha"] = SHA_A
        state["audit_target_sha"] = SHA_B
        dump(self.state_path, state)
        before = self.state_path.read_bytes()

        with self.assertRaisesRegex(HandoffError, "builder_head_sha must equal audit_target_sha"):
            audit_handoff(self.state_path, audit_path)

        self.assertEqual(before, self.state_path.read_bytes())

    def test_audited_states_require_consistent_relevant_shas(self):
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state.update(
            machine_state="FIX_REQUIRED",
            next_actor="BUILDER",
            builder_head_sha=SHA_A,
            audit_target_sha=SHA_A,
            last_audited_sha=SHA_B,
            last_audit_result="FAIL",
        )
        dump(self.state_path, state)
        before = self.state_path.read_bytes()

        with self.assertRaisesRegex(HandoffError, "last_audited_sha must equal audit_target_sha"):
            status(self.state_path)

        self.assertEqual(before, self.state_path.read_bytes())

    def test_machine_states_require_their_relevant_shas(self):
        initial = self.state_path.read_bytes()
        blocked_report = self.root / "builder-blocked-sha-requirement.json"
        dump(blocked_report, builder_report(result="BLOCKED"))
        cases = (
            ("READY_FOR_BUILD", {"builder_head_sha": SHA_A}, "all relevant SHAs to be null"),
            ("READY_FOR_AUDIT", {}, "requires builder_head_sha"),
            ("AUDITING", {}, "requires builder_head_sha"),
            ("FIX_REQUIRED", {"builder_head_sha": SHA_A, "audit_target_sha": SHA_A}, "requires last_audited_sha"),
            ("WAITING_PRODUCT_AUTHORITY", {"builder_head_sha": SHA_A, "audit_target_sha": SHA_A}, "requires last_audited_sha"),
            ("GATE_APPROVED", {"builder_head_sha": SHA_A, "audit_target_sha": SHA_A}, "requires last_audited_sha"),
            (
                "BLOCKED",
                {
                    "blocked_reason": {"code": "BUILDER_BLOCKED", "source": "BUILDER", "evidence_ref": "last_builder_report"},
                    "last_builder_report": str(blocked_report),
                },
                "requires builder_head_sha",
            ),
        )

        for machine_state, changes, expected_error in cases:
            with self.subTest(machine_state=machine_state):
                state = json.loads(initial)
                state.update(machine_state=machine_state, **changes)
                state["next_actor"] = {
                    "READY_FOR_BUILD": "BUILDER",
                    "READY_FOR_AUDIT": "AUDITOR",
                    "AUDITING": "AUDITOR",
                    "FIX_REQUIRED": "BUILDER",
                    "WAITING_PRODUCT_AUTHORITY": "PRODUCT_AUTHORITY",
                    "GATE_APPROVED": "STOP",
                    "BLOCKED": "PRODUCT_AUTHORITY",
                }[machine_state]
                dump(self.state_path, state)
                before = self.state_path.read_bytes()

                with self.assertRaisesRegex(HandoffError, expected_error):
                    status(self.state_path)

                self.assertEqual(before, self.state_path.read_bytes())
                self.state_path.write_bytes(initial)

    def test_relevant_shas_survive_handoffs_and_reload(self):
        builder_path = self.root / "builder-shas.json"
        audit_path = self.root / "audit-shas.json"
        dump(builder_path, builder_report(sha=SHA_A))
        dump(audit_path, audit_report(SHA_A, result="FAIL", blocking=True))

        built = builder_handoff(self.state_path, builder_path, SHA_A)
        audited = audit_handoff(self.state_path, audit_path)
        restarted = status(self.state_path)

        self.assertEqual((SHA_A, SHA_A, None), (
            built["builder_head_sha"], built["audit_target_sha"], built["last_audited_sha"]
        ))
        self.assertEqual((SHA_A, SHA_A, SHA_A), (
            audited["builder_head_sha"], audited["audit_target_sha"], audited["last_audited_sha"]
        ))
        self.assertEqual((SHA_A, SHA_A, SHA_A), (
            restarted["builder_head_sha"], restarted["audit_target_sha"], restarted["last_audited_sha"]
        ))

    def test_fail_fix_pass_human_gate_flow(self):
        builder_one = self.root / "builder-1.json"
        dump(builder_one, builder_report())
        state = builder_handoff(self.state_path, builder_one, SHA_A)
        self.assertEqual("READY_FOR_AUDIT", state["machine_state"])
        self.assertEqual(SHA_A, state["audit_target_sha"])

        audit_one = self.root / "audit-1.json"
        dump(audit_one, audit_report(SHA_A, result="FAIL", blocking=True))
        state = audit_handoff(self.state_path, audit_one)
        self.assertEqual("FIX_REQUIRED", state["machine_state"])
        self.assertEqual(1, state["audit_round"])

        builder_two = self.root / "builder-2.json"
        dump(builder_two, builder_report(sha=SHA_B))
        state = builder_handoff(self.state_path, builder_two, SHA_B)
        self.assertEqual(SHA_B, state["audit_target_sha"])

        audit_two = self.root / "audit-2.json"
        dump(audit_two, audit_report(SHA_B))
        state = audit_handoff(self.state_path, audit_two)
        self.assertEqual("WAITING_PRODUCT_AUTHORITY", state["machine_state"])
        self.assertIsNone(state["approval"])
        self.assertEqual("G01", state["gate"])
        self.assertEqual("PRODUCT_AUTHORITY", next_actor(state))

        state = approve_gate(self.state_path, executor_id="human-owner", role="PRODUCT_AUTHORITY", gate="G01", audited_sha=SHA_B)
        self.assertEqual("GATE_APPROVED", state["machine_state"])
        self.assertEqual(SHA_B, state["approval"]["audited_sha"])

    def test_fail_transition_preserves_all_o0_c14_invariants(self):
        builder_path = self.root / "builder-o0-c14.json"
        audit_path = self.root / "audit-o0-c14.json"
        dump(builder_path, builder_report())
        dump(audit_path, audit_report(SHA_A, result="FAIL", blocking=True))
        before = builder_handoff(self.state_path, builder_path, SHA_A)

        result = audit_handoff(self.state_path, audit_path)

        self.assertEqual("FIX_REQUIRED", result["machine_state"])
        self.assertEqual("BUILDER", result["next_actor"])
        self.assertEqual(before["audit_round"] + 1, result["audit_round"])
        self.assertEqual(SHA_A, result["audit_target_sha"])
        self.assertEqual(SHA_A, result["last_audited_sha"])
        self.assertEqual("FAIL", result["last_audit_result"])
        self.assertIsNone(result["approval"])
        self.assertEqual("G01", result["gate"])

    def test_audit_sha_mismatch_is_rejected(self):
        builder_path = self.root / "builder.json"
        dump(builder_path, builder_report())
        builder_handoff(self.state_path, builder_path, SHA_A)
        audit_path = self.root / "audit.json"
        dump(audit_path, audit_report(SHA_B))
        with self.assertRaises(HandoffError):
            audit_handoff(self.state_path, audit_path)

    def test_disputed_builder_report_requires_finding_reference(self):
        disputed_path = self.root / "builder-disputed.json"
        dump(disputed_path, builder_report(result="DISPUTED"))
        before = self.state_path.read_bytes()

        with self.assertRaises(HandoffError):
            builder_handoff(self.state_path, disputed_path, SHA_A)

        self.assertEqual(before, self.state_path.read_bytes())

    def test_disputed_builder_report_blocks_for_product_authority(self):
        disputed_path = self.root / "builder-disputed.json"
        report = builder_report(result="DISPUTED")
        report["disputed_findings"] = ["AUD-020-001"]
        dump(disputed_path, report)

        state = builder_handoff(self.state_path, disputed_path, SHA_A)

        self.assertEqual("BLOCKED", state["machine_state"])
        self.assertEqual("PRODUCT_AUTHORITY", next_actor(state))
        self.assertEqual(str(disputed_path), state["last_builder_report"])
        self.assertIsNone(state["approval"])
        self.assertIn("DISPUTED", state["message"])
        self.assertEqual(
            {"code": "BUILDER_DISPUTED", "source": "BUILDER", "evidence_ref": "last_builder_report"},
            state.get("blocked_reason"),
        )
        self.assertEqual(state["blocked_reason"], status(self.state_path)["blocked_reason"])

    def test_blocked_builder_report_persists_structured_reason(self):
        report_path = self.root / "builder-blocked.json"
        dump(report_path, builder_report(result="BLOCKED"))

        state = builder_handoff(self.state_path, report_path, SHA_A)

        self.assertEqual(
            {"code": "BUILDER_BLOCKED", "source": "BUILDER", "evidence_ref": "last_builder_report"},
            state.get("blocked_reason"),
        )

    def test_audit_escalation_persists_safe_structured_reason(self):
        builder_path = self.root / "builder-escalate.json"
        audit_path = self.root / "audit-escalate.json"
        dump(builder_path, builder_report())
        report = audit_report(SHA_A, result="ESCALATE")
        report["escalation_reason"] = "secret-token-must-not-be-copied"
        dump(audit_path, report)
        builder_handoff(self.state_path, builder_path, SHA_A)

        state = audit_handoff(self.state_path, audit_path)

        self.assertEqual(
            {"code": "AUDITOR_ESCALATED", "source": "AUDITOR", "evidence_ref": "last_audit_report"},
            state.get("blocked_reason"),
        )
        self.assertNotIn("secret-token-must-not-be-copied", self.state_path.read_text(encoding="utf-8"))

    def test_pass_with_blocking_finding_is_rejected(self):
        builder_path = self.root / "builder.json"
        dump(builder_path, builder_report())
        builder_handoff(self.state_path, builder_path, SHA_A)
        audit_path = self.root / "audit.json"
        dump(audit_path, audit_report(SHA_A, result="PASS", blocking=True))
        with self.assertRaises(HandoffError):
            audit_handoff(self.state_path, audit_path)

    def test_same_builder_and_auditor_role_is_rejected(self):
        bad_policy = self.root / "bad-policy.json"
        other_state = self.root / "other-state.json"
        dump(bad_policy, policy(builder="same", auditor="same"))
        with self.assertRaises(HandoffError):
            init_state(
                bad_policy,
                other_state,
                project_id="sample",
                phase="F01",
                gate="G01",
                builder_branch="work/sample-f01",
            )

    def test_same_executor_for_builder_and_auditor_is_rejected(self):
        builder_path = self.root / "builder.json"
        report = builder_report()
        dump(builder_path, report)
        builder_handoff(self.state_path, builder_path, SHA_A)
        audit_path = self.root / "audit.json"
        audit = audit_report(SHA_A)
        audit["executor_id"] = report["executor_id"]
        dump(audit_path, audit)
        with self.assertRaises(HandoffError):
            audit_handoff(self.state_path, audit_path)

    def test_pass_with_fail_check_is_rejected(self):
        self._assert_pass_check_rejected("FAIL")

    def test_pass_with_not_run_check_is_rejected(self):
        self._assert_pass_check_rejected("NOT_RUN")

    def _assert_pass_check_rejected(self, status):
        builder_path = self.root / f"builder-{status}.json"
        dump(builder_path, builder_report())
        builder_handoff(self.state_path, builder_path, SHA_A)
        audit_path = self.root / f"audit-{status}.json"
        report = audit_report(SHA_A)
        report["checks"][0]["status"] = status
        dump(audit_path, report)
        with self.assertRaises(HandoffError):
            audit_handoff(self.state_path, audit_path)

    def test_fail_without_findings_is_rejected(self):
        builder_path = self.root / "builder-empty-findings.json"
        dump(builder_path, builder_report())
        builder_handoff(self.state_path, builder_path, SHA_A)
        audit_path = self.root / "audit-empty-findings.json"
        dump(audit_path, audit_report(SHA_A, result="FAIL"))
        before = self.state_path.read_bytes()
        with self.assertRaises(HandoffError):
            audit_handoff(self.state_path, audit_path)
        self.assertEqual(before, self.state_path.read_bytes())

    def test_malformed_fail_is_rejected_without_state_mutation(self):
        builder_path = self.root / "builder-malformed-fail.json"
        audit_path = self.root / "audit-malformed-fail.json"
        dump(builder_path, builder_report())
        builder_handoff(self.state_path, builder_path, SHA_A)
        malformed = audit_report(SHA_A, result="FAIL", blocking=True)
        del malformed["executor_id"]
        dump(audit_path, malformed)
        before = self.state_path.read_bytes()

        with self.assertRaises(HandoffError):
            audit_handoff(self.state_path, audit_path)

        self.assertEqual(before, self.state_path.read_bytes())

    def test_unauthorized_gate_requester_is_rejected(self):
        builder_path = self.root / "builder-approval.json"
        audit_path = self.root / "audit-approval.json"
        dump(builder_path, builder_report())
        builder_handoff(self.state_path, builder_path, SHA_A)
        dump(audit_path, audit_report(SHA_A))
        audit_handoff(self.state_path, audit_path)
        with self.assertRaises(HandoffError):
            approve_gate(self.state_path, executor_id="runner", role="PRODUCT_AUTHORITY", gate="G01", audited_sha=SHA_A)

    def test_auditor_gate_registration_attempt_is_rejected_without_state_change(self):
        builder_path = self.root / "builder-gate-attempt.json"
        audit_path = self.root / "audit-gate-attempt.json"
        dump(builder_path, builder_report())
        builder_handoff(self.state_path, builder_path, SHA_A)
        report = audit_report(SHA_A)
        report["gate_registration"] = "G01"
        dump(audit_path, report)
        before = self.state_path.read_bytes()

        with self.assertRaisesRegex(HandoffError, "audit schema validation failed"):
            audit_handoff(self.state_path, audit_path)

        self.assertEqual(before, self.state_path.read_bytes())
        unchanged = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual("READY_FOR_AUDIT", unchanged["machine_state"])
        self.assertIsNone(unchanged["approval"])
        self.assertEqual("G01", unchanged["gate"])

    def test_third_failed_audit_blocks_default_three_round_loop(self):
        for round_number, sha in enumerate((SHA_A, SHA_B, SHA_C), start=1):
            builder_path = self.root / f"builder-round-{round_number}.json"
            audit_path = self.root / f"audit-round-{round_number}.json"
            dump(builder_path, builder_report(sha=sha))
            dump(audit_path, audit_report(sha, result="FAIL", blocking=True))
            builder_handoff(self.state_path, builder_path, sha)
            state = audit_handoff(self.state_path, audit_path)

            self.assertLessEqual(state["audit_round"], state["max_audit_rounds"])

            if round_number < 3:
                self.assertEqual("FIX_REQUIRED", state["machine_state"])

        self.assertEqual(3, state["audit_round"])
        self.assertEqual("BLOCKED", state["machine_state"])
        self.assertEqual("PRODUCT_AUTHORITY", next_actor(state))
        self.assertIsNone(state["approval"])
        self.assertEqual(
            {"code": "AUDIT_ROUND_LIMIT_REACHED", "source": "ORCHESTRATOR", "evidence_ref": "orchestrator_state"},
            state.get("blocked_reason"),
        )

    def test_blocked_without_reason_is_rejected_without_mutation(self):
        report_path = self.root / "builder-blocked-without-reason.json"
        dump(report_path, builder_report(result="BLOCKED"))
        blocked = builder_handoff(self.state_path, report_path, SHA_A)
        blocked["blocked_reason"] = None
        dump(self.state_path, blocked)
        before = self.state_path.read_bytes()

        with self.assertRaisesRegex(HandoffError, "BLOCKED requires blocked_reason"):
            status(self.state_path)

        self.assertEqual(before, self.state_path.read_bytes())

    def test_blocked_with_reason_inconsistent_with_transition_is_rejected(self):
        report_path = self.root / "builder-blocked-wrong-reason.json"
        dump(report_path, builder_report(result="BLOCKED"))
        blocked = builder_handoff(self.state_path, report_path, SHA_A)
        blocked["blocked_reason"] = {
            "code": "AUDITOR_ESCALATED",
            "source": "AUDITOR",
            "evidence_ref": "last_audit_report",
        }
        dump(self.state_path, blocked)
        before = self.state_path.read_bytes()

        with self.assertRaisesRegex(HandoffError, "AUDITOR_ESCALATED requires audit escalation"):
            status(self.state_path)

        self.assertEqual(before, self.state_path.read_bytes())

    def test_builder_blocked_reason_must_match_referenced_report_result(self):
        initial = self.state_path.read_bytes()
        cases = (
            ("BLOCKED", "BUILDER_DISPUTED"),
            ("DISPUTED", "BUILDER_BLOCKED"),
        )

        for result, wrong_code in cases:
            with self.subTest(result=result, wrong_code=wrong_code):
                self.state_path.write_bytes(initial)
                report_path = self.root / f"builder-{result.lower()}-swapped-reason.json"
                report = builder_report(result=result)
                if result == "DISPUTED":
                    report["disputed_findings"] = ["AUD-C34-001"]
                dump(report_path, report)
                blocked = builder_handoff(self.state_path, report_path, SHA_A)
                blocked["blocked_reason"] = {
                    "code": wrong_code,
                    "source": "BUILDER",
                    "evidence_ref": "last_builder_report",
                }
                dump(self.state_path, blocked)
                before = self.state_path.read_bytes()

                with self.assertRaisesRegex(HandoffError, "blocked_reason does not match Builder result"):
                    status(self.state_path)

                self.assertEqual(before, self.state_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
