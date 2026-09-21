from __future__ import annotations

import json
import re
import sqlite3
import unittest
from pathlib import Path

from geosafe.service import GeoSafeService, NotFoundError
from tests.helpers import DemoApplication


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class StrictScopeTests(unittest.TestCase):
    def test_schema_contains_only_approved_domain_tables(self) -> None:
        application = DemoApplication()
        try:
            connection = sqlite3.connect(application.database_path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                        """
                    )
                }
            finally:
                connection.close()
            self.assertEqual(
                tables,
                {
                    "barangays",
                    "municipal_boundary",
                    "hazard_datasets",
                    "hazard_features",
                    "historical_incidents",
                    "clup_references",
                    "fuzzy_models",
                    "fuzzy_variables",
                    "membership_functions",
                    "fuzzy_rules",
                    "assessments",
                    "assessment_inputs",
                    "assessment_memberships",
                    "assessment_rule_activations",
                    "assessment_results",
                    "generated_reports",
                    "routing_study_areas",
                    "evacuation_centers",
                    "searchable_locations",
                    "visitor_sessions",
                    "visitor_events",
                    "admin_audit_log",
                },
            )
        finally:
            application.close()

    def test_no_forbidden_tables_are_declared(self) -> None:
        schema = (PROJECT_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        forbidden = {
            "users",
            "roles",
            "user_roles",
            "permissions",
            "staff_profiles",
            "user_sessions",
            "administrative_approvals",
            "audit_logs",
            "activity_logs",
            "permission_matrices",
            "dataset_approvals",
        }
        declared = set(
            re.findall(
                r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_]+)",
                schema,
                flags=re.IGNORECASE,
            )
        )
        self.assertTrue(forbidden.isdisjoint(declared))

    def test_user_role_and_admin_api_groups_do_not_exist(self) -> None:
        application = DemoApplication()
        try:
            for path in (
                "/api/v1/users",
                "/api/v1/roles",
                "/api/v1/permissions",
                "/api/v1/admin",
                "/api/v1/staff",
                "/api/v1/audit-logs",
                "/api/v1/activity",
                "/api/v1/sessions",
                "/api/v1/approvals",
                "/api/v1/datasets/upload",
                "/api/v1/datasets/publish",
                "/api/v1/fuzzy-models/editor",
                "/api/v1/auth/register",
                "/api/v1/auth/login",
            ):
                response = application.api.dispatch("GET", path)
                self.assertEqual(
                    response.status,
                    404,
                    msg=f"Out-of-scope route unexpectedly exists: {path}",
                )
                self.assertEqual(
                    json.loads(response.body)["error"]["code"], "not_found"
                )
        finally:
            application.close()

    def test_demo_repository_hazards_require_explicit_test_opt_in(self) -> None:
        application = DemoApplication()
        try:
            restricted = GeoSafeService(
                application.repository,
                application.model,
                allow_test_fixtures=False,
            )
            self.assertEqual(restricted.hazard_layers()["items"], [])
            demo_dataset = application.repository.hazard_datasets()[0]
            with self.assertRaises(NotFoundError):
                restricted.hazard_layer_features(demo_dataset["id"])
        finally:
            application.close()


if __name__ == "__main__":
    unittest.main()
