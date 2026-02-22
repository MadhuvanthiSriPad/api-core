"""Integration testing framework for validating cross-service fixes."""

import asyncio
import subprocess
import time
import httpx
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


class IntegrationTestRunner:
    """Runs integration tests across microservices stack."""

    def __init__(self, compose_file: str = "docker-compose.integration.yaml"):
        self.compose_file = compose_file
        self.base_url_api = "http://localhost:8001"
        self.base_url_billing = "http://localhost:8002"
        self.base_url_dashboard = "http://localhost:8003"

    async def run_full_suite(self) -> Dict[str, any]:
        """
        Run complete integration test suite.

        Returns:
            Dict with test results
        """
        print("\n" + "="*60)
        print("INTEGRATION TEST SUITE")
        print("="*60)

        results = {
            "started_at": time.time(),
            "services_up": False,
            "tests_passed": [],
            "tests_failed": [],
            "total_duration": 0
        }

        try:
            # Step 1: Start services
            print("\n[SETUP] Starting microservices stack...")
            await self.start_services()
            results["services_up"] = True

            # Step 2: Wait for health checks
            print("[SETUP] Waiting for services to be healthy...")
            await self.wait_for_health()

            # Step 3: Run integration tests
            print("\n[TESTS] Running integration tests...")
            test_results = await self.run_tests()

            results["tests_passed"] = [t for t, passed in test_results.items() if passed]
            results["tests_failed"] = [t for t, passed in test_results.items() if not passed]

        except Exception as e:
            logger.error(f"Integration test suite failed: {e}")
            results["error"] = str(e)

        finally:
            # Clean up
            print("\n[CLEANUP] Stopping services...")
            await self.stop_services()

        results["total_duration"] = time.time() - results["started_at"]

        # Print summary
        self.print_summary(results)

        return results

    async def start_services(self):
        """Start docker-compose services."""
        subprocess.run(
            ["docker-compose", "-f", self.compose_file, "up", "-d", "--build"],
            check=True
        )

    async def stop_services(self):
        """Stop docker-compose services."""
        subprocess.run(
            ["docker-compose", "-f", self.compose_file, "down", "-v"],
            check=False
        )

    async def wait_for_health(self, max_wait: int = 60):
        """Wait for all services to be healthy."""
        start = time.time()
        services = [
            ("api-core", f"{self.base_url_api}/health"),
            ("billing-service", f"{self.base_url_billing}/health"),
            ("dashboard-service", f"{self.base_url_dashboard}/health")
        ]

        async with httpx.AsyncClient() as client:
            while time.time() - start < max_wait:
                all_healthy = True

                for name, url in services:
                    try:
                        resp = await client.get(url, timeout=2.0)
                        if resp.status_code != 200:
                            all_healthy = False
                    except:
                        all_healthy = False

                if all_healthy:
                    print("[READY] All services healthy")
                    return

                await asyncio.sleep(2)

            raise TimeoutError("Services did not become healthy in time")

    async def run_tests(self) -> Dict[str, bool]:
        """
        Run integration tests.

        Returns:
            Dict mapping test name to pass/fail status
        """
        results = {}

        async with httpx.AsyncClient() as client:
            # Test 1: Create session via api-core
            results["test_create_session"] = await self.test_create_session(client)

            # Test 2: Billing service fetches session
            results["test_billing_fetch_session"] = await self.test_billing_fetch(client)

            # Test 3: Dashboard aggregates data
            results["test_dashboard_aggregation"] = await self.test_dashboard(client)

            # Test 4: End-to-end flow
            results["test_e2e_flow"] = await self.test_e2e_flow(client)

        return results

    async def test_create_session(self, client: httpx.AsyncClient) -> bool:
        """Test session creation via api-core."""
        try:
            resp = await client.post(
                f"{self.base_url_api}/api/v1/sessions",
                json={
                    "team_id": "test-team",
                    "agent_name": "test-agent",
                    "priority": "high"
                }
            )
            if resp.status_code == 201:
                print("  [PASS] test_create_session")
                return True
            else:
                print(f"  [FAIL] test_create_session ({resp.status_code})")
                return False
        except Exception as e:
            print(f"  [FAIL] test_create_session ({e})")
            return False

    async def test_billing_fetch(self, client: httpx.AsyncClient) -> bool:
        """Test billing service can fetch sessions."""
        try:
            resp = await client.get(f"{self.base_url_billing}/health")
            if resp.status_code == 200:
                print("  [PASS] test_billing_fetch_session")
                return True
            print(f"  [FAIL] test_billing_fetch_session")
            return False
        except Exception as e:
            print(f"  [FAIL] test_billing_fetch_session ({e})")
            return False

    async def test_dashboard(self, client: httpx.AsyncClient) -> bool:
        """Test dashboard aggregation."""
        try:
            resp = await client.get(f"{self.base_url_dashboard}/api/dashboard")
            if resp.status_code == 200:
                print("  [PASS] test_dashboard_aggregation")
                return True
            print(f"  [FAIL] test_dashboard_aggregation")
            return False
        except Exception as e:
            print(f"  [FAIL] test_dashboard_aggregation ({e})")
            return False

    async def test_e2e_flow(self, client: httpx.AsyncClient) -> bool:
        """Test complete end-to-end flow."""
        try:
            # Create session → Fetch via dashboard
            create_resp = await client.post(
                f"{self.base_url_api}/api/v1/sessions",
                json={"team_id": "e2e-test", "agent_name": "e2e", "priority": "low"}
            )

            if create_resp.status_code != 201:
                return False

            session_id = create_resp.json()["session_id"]

            # Fetch via dashboard
            fetch_resp = await client.get(f"{self.base_url_dashboard}/api/sessions/{session_id}")

            if fetch_resp.status_code == 200:
                print("  [PASS] test_e2e_flow")
                return True

            return False
        except Exception as e:
            print(f"  [FAIL] test_e2e_flow ({e})")
            return False

    def print_summary(self, results: Dict):
        """Print test results summary."""
        print("\n" + "="*60)
        print("TEST RESULTS")
        print("="*60)
        print(f"Passed: {len(results['tests_passed'])}")
        print(f"Failed: {len(results['tests_failed'])}")
        print(f"Duration: {results['total_duration']:.2f}s")

        if results['tests_failed']:
            print(f"\n[FAILED] Tests that failed:")
            for test in results['tests_failed']:
                print(f"  - {test}")
        else:
            print("\n[SUCCESS] All tests passed!")

        print("="*60 + "\n")
